"""Offline tests for ResponsesProvider (R1.5-U01/U02). Upstream is always MockTransport."""

# ruff: noqa: E501 - SSE fixture payloads are single long data lines by protocol.
from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

import httpx
import pytest

from cgbot.provider import (
    BadRequest,
    Completed,
    FunctionCall,
    ItemDone,
    ModelProvider,
    ModelRequest,
    ProviderError,
    RateLimited,
    Reasoning,
    TextDelta,
    TextOutput,
    UpstreamUnavailable,
    Usage,
    function_call_output,
    item_from_output,
    user_message,
)
from cgbot.provider.responses import ResponsesProvider

KEY = "sk-test-SECRET-9f3a71"
BASE = "https://upstream.example/v1/"
ResultCalls = list[tuple[int | None, Exception | None]]


def make_provider(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    on_result: Callable[[int | None, Exception | None], None] | None = None,
) -> ResponsesProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return ResponsesProvider(
        BASE,
        KEY,
        "test-model",
        128000,
        http=client,
        timeout=5.0,
        on_result=on_result,
        retry_attempts=0,
    )


def json_handler(
    payload: Any, status: int = 200, headers: dict[str, str] | None = None
) -> tuple[Callable[[httpx.Request], httpx.Response], list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=payload, headers=headers)

    return handler, seen


def sse_handler(
    text: str, status: int = 200
) -> tuple[Callable[[httpx.Request], httpx.Response], list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            status,
            content=text.encode("utf-8"),
            headers={"content-type": "text/event-stream"},
        )

    return handler, seen


def request_of(**overrides: Any) -> ModelRequest:
    fields: dict[str, Any] = {
        "instructions": "你是助手。",
        "input": [user_message("你好")],
    }
    fields.update(overrides)
    return ModelRequest(**fields)


USAGE = {
    "input_tokens": 120,
    "output_tokens": 30,
    "input_tokens_details": {"cached_tokens": 64},
    "output_tokens_details": {"reasoning_tokens": 20},
}

TEXT_RESPONSE: dict[str, Any] = {
    "id": "resp_1",
    "object": "response",
    "status": "completed",
    "output": [
        {
            "type": "reasoning",
            "id": "rs_1",
            "summary": [{"type": "summary_text", "text": "先想想"}],
            "encrypted_content": "ENC-OPAQUE",
        },
        {
            "type": "message",
            "id": "msg_1",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "你好", "annotations": []}],
        },
    ],
    "output_text": "你好",
    "usage": USAGE,
}


async def test_text_response_parses_items_usage_and_request_shape() -> None:
    handler, seen = json_handler(TEXT_RESPONSE)
    provider = make_provider(handler)

    result = await provider.respond(request_of())

    assert result.response_id == "resp_1"
    assert result.status == "completed"
    assert result.incomplete_reason is None
    assert result.usage == Usage(input_tokens=120, output_tokens=30, cached=64, reasoning=20)
    assert isinstance(result.items[0], Reasoning)
    assert result.items[0].summary == "先想想"
    assert result.items[0].raw["encrypted_content"] == "ENC-OPAQUE"
    assert result.items[1] == TextOutput(text="你好", raw=TEXT_RESPONSE["output"][1])

    (sent,) = seen
    assert str(sent.url) == "https://upstream.example/v1/responses"
    assert sent.headers["authorization"] == f"Bearer {KEY}"
    body = json.loads(sent.content)
    assert body["model"] == "test-model"
    assert body["instructions"] == "你是助手。"
    assert body["store"] is False
    assert body["stream"] is False
    assert "tools" not in body
    assert "reasoning" not in body
    assert "max_output_tokens" not in body


async def test_request_options_are_passed_through() -> None:
    handler, seen = json_handler(TEXT_RESPONSE)
    provider = make_provider(handler)
    tool = {"type": "function", "name": "reply", "parameters": {"type": "object"}}

    await provider.respond(request_of(tools=[tool], reasoning_effort="max", max_output_tokens=512))

    body = json.loads(seen[0].content)
    assert body["tools"] == [tool]
    assert body["reasoning"] == {"effort": "max"}
    assert body["max_output_tokens"] == 512


async def test_function_call_parses_arguments() -> None:
    payload = {
        "id": "resp_2",
        "status": "completed",
        "output": [
            {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "get_weather",
                "arguments": '{"city": "北京"}',
                "status": "completed",
            }
        ],
        "usage": USAGE,
    }
    handler, _ = json_handler(payload)

    result = await make_provider(handler).respond(request_of())

    (call,) = result.items
    assert isinstance(call, FunctionCall)
    assert call.call_id == "call_1"
    assert call.name == "get_weather"
    assert call.arguments == '{"city": "北京"}'
    assert call.parsed_arguments == {"city": "北京"}
    assert call.parse_error is None


@pytest.mark.parametrize(
    ("arguments", "expected_error_fragment"),
    [
        ('{"city": "北京"', "invalid JSON"),
        ("[1, 2]", "not a JSON object"),
    ],
)
async def test_invalid_arguments_kept_raw_with_error_flag(
    arguments: str, expected_error_fragment: str
) -> None:
    payload = {
        "id": "resp_bad",
        "status": "completed",
        "output": [
            {
                "type": "function_call",
                "call_id": "call_bad",
                "name": "get_weather",
                "arguments": arguments,
            }
        ],
    }
    handler, _ = json_handler(payload)

    result = await make_provider(handler).respond(request_of())

    (call,) = result.items
    assert isinstance(call, FunctionCall)
    assert call.arguments == arguments
    assert call.parsed_arguments is None
    assert call.parse_error is not None
    assert expected_error_fragment in call.parse_error
    assert "北京" not in call.parse_error


async def test_empty_arguments_parse_as_empty_object() -> None:
    payload = {
        "id": "resp_e",
        "status": "completed",
        "output": [{"type": "function_call", "call_id": "c", "name": "ping", "arguments": ""}],
    }
    handler, _ = json_handler(payload)

    (call,) = (await make_provider(handler).respond(request_of())).items

    assert isinstance(call, FunctionCall)
    assert call.parsed_arguments == {}
    assert call.parse_error is None


async def test_incomplete_status_is_returned_not_raised() -> None:
    payload = {
        "id": "resp_3",
        "status": "incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "部分"}],
            }
        ],
    }
    handler, _ = json_handler(payload)

    result = await make_provider(handler).respond(request_of())

    assert result.status == "incomplete"
    assert result.incomplete_reason == "max_output_tokens"
    assert result.items == (TextOutput(text="部分", raw=payload["output"][0]),)
    assert result.usage == Usage()


async def test_missing_status_is_a_provider_error() -> None:
    handler, _ = json_handler({"id": "resp_x", "output": []})
    provider = make_provider(handler)

    with pytest.raises(ProviderError, match="no status"):
        await provider.respond(request_of())


async def test_rate_limited_with_retry_after_seconds() -> None:
    handler, _ = json_handler(
        {"error": {"message": "slow down"}}, status=429, headers={"retry-after": "7"}
    )
    calls: ResultCalls = []
    provider = make_provider(handler, on_result=lambda s, e: calls.append((s, e)))

    with pytest.raises(RateLimited) as info:
        await provider.respond(request_of())

    assert info.value.retry_after == 7.0
    assert info.value.status == 429
    assert "slow down" in info.value.body
    assert calls == [(429, info.value)]


async def test_rate_limited_with_retry_after_http_date() -> None:
    when = datetime.now(UTC) + timedelta(seconds=30)
    handler, _ = json_handler(
        {}, status=429, headers={"retry-after": format_datetime(when, usegmt=True)}
    )

    with pytest.raises(RateLimited) as info:
        await make_provider(handler).respond(request_of())

    assert info.value.retry_after is not None
    assert info.value.retry_after == pytest.approx(30, abs=5)


async def test_rate_limited_without_retry_after() -> None:
    handler, _ = json_handler({}, status=429)

    with pytest.raises(RateLimited) as info:
        await make_provider(handler).respond(request_of())

    assert info.value.retry_after is None


async def test_server_error_is_unavailable_with_status_and_body() -> None:
    handler, _ = json_handler({"error": "upstream overloaded"}, status=503)
    calls: ResultCalls = []
    provider = make_provider(handler, on_result=lambda s, e: calls.append((s, e)))

    with pytest.raises(UpstreamUnavailable) as info:
        await provider.respond(request_of())

    assert info.value.status == 503
    assert "overloaded" in info.value.body
    assert len(calls) == 1
    assert calls[0][0] == 503
    assert calls[0][1] is info.value


async def test_client_error_is_bad_request() -> None:
    handler, _ = json_handler({"error": {"message": "unknown param"}}, status=400)

    with pytest.raises(BadRequest) as info:
        await make_provider(handler).respond(request_of())

    assert info.value.status == 400
    assert "unknown param" in info.value.body


async def test_timeout_is_unavailable_with_no_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow upstream", request=request)

    calls: ResultCalls = []
    provider = make_provider(handler, on_result=lambda s, e: calls.append((s, e)))

    with pytest.raises(UpstreamUnavailable, match="timed out"):
        await provider.respond(request_of())

    assert len(calls) == 1
    assert calls[0][0] is None
    assert isinstance(calls[0][1], UpstreamUnavailable)


async def test_network_error_is_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(UpstreamUnavailable, match="unreachable"):
        await make_provider(handler).respond(request_of())


async def test_non_json_success_body_is_provider_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>gateway</html>")

    with pytest.raises(ProviderError, match="non-JSON") as info:
        await make_provider(handler).respond(request_of())

    assert info.value.status == 200


async def test_api_key_never_appears_in_repr_or_errors() -> None:
    echo = f"bad key {KEY}"
    handler, _ = json_handler({"error": echo}, status=401)
    calls: ResultCalls = []
    provider = make_provider(handler, on_result=lambda s, e: calls.append((s, e)))

    with pytest.raises(BadRequest) as info:
        await provider.respond(request_of())

    err = info.value
    for text in (repr(provider), str(provider), repr(err), str(err), err.body, repr(calls)):
        assert KEY not in text
    assert "***" in err.body
    assert KEY not in repr(ResponsesProvider(BASE, KEY, "m", 1))


async def test_api_key_scrubbed_from_server_error_body() -> None:
    handler, _ = json_handler({"detail": f"auth={KEY}"}, status=500)

    with pytest.raises(UpstreamUnavailable) as info:
        await make_provider(handler).respond(request_of())

    assert KEY not in str(info.value)
    assert KEY not in repr(info.value)
    assert KEY not in info.value.body


async def test_success_reports_to_hook_and_properties() -> None:
    handler, _ = json_handler(TEXT_RESPONSE)
    calls: ResultCalls = []
    provider = make_provider(handler, on_result=lambda s, e: calls.append((s, e)))

    assert provider.name == "test-model"
    assert provider.context_window == 128000
    assert calls == []
    await provider.respond(request_of())
    assert calls == [(200, None)]


def test_provider_satisfies_protocol_and_rejects_bad_window() -> None:
    provider: ModelProvider = make_provider(json_handler(TEXT_RESPONSE)[0])
    assert provider.context_window == 128000
    with pytest.raises(ValueError, match="context_window"):
        ResponsesProvider(BASE, KEY, "m", 0)


async def test_tool_round_trip_body_uses_pass_through_items() -> None:
    first_payload = {
        "id": "resp_t1",
        "status": "completed",
        "output": [
            {"type": "reasoning", "id": "rs_t", "summary": [], "encrypted_content": "ENC-T"},
            {
                "type": "function_call",
                "id": "fc_t",
                "call_id": "call_t",
                "name": "reply",
                "arguments": '{"text": "嗨"}',
                "status": "completed",
            },
        ],
    }
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        payload = first_payload if len(seen) == 1 else TEXT_RESPONSE
        return httpx.Response(200, json=payload)

    provider = make_provider(handler)
    first = await provider.respond(request_of())
    history = [user_message("你好"), *(item_from_output(i) for i in first.items)]
    history.append(function_call_output("call_t", "ok"))

    await provider.respond(request_of(input=history))

    sent_input = json.loads(seen[1].content)["input"]
    assert sent_input[1] == {"type": "reasoning", "summary": [], "encrypted_content": "ENC-T"}
    assert sent_input[2] == {
        "type": "function_call",
        "call_id": "call_t",
        "name": "reply",
        "arguments": '{"text": "嗨"}',
    }
    assert sent_input[3] == {"type": "function_call_output", "call_id": "call_t", "output": "ok"}


# --- streaming -------------------------------------------------------------------------------

STREAM_TOOL_CALL = """\
event: response.created
data: {"type":"response.created","response":{"id":"resp_s1","status":"in_progress","output":[]}}

event: response.output_item.added
data: {"type":"response.output_item.added","output_index":0,"item":{"type":"message","id":"msg_s1","role":"assistant","content":[],"status":"in_progress"}}

event: response.output_text.delta
data: {"type":"response.output_text.delta","item_id":"msg_s1","output_index":0,"delta":"你"}

event: response.output_text.delta
data: {"type":"response.output_text.delta","item_id":"msg_s1","output_index":0,"delta":"好"}

event: response.output_item.done
data: {"type":"response.output_item.done","output_index":0,"item":{"type":"message","id":"msg_s1","role":"assistant","status":"completed","content":[{"type":"output_text","text":"你好","annotations":[]}]}}

event: response.output_item.added
data: {"type":"response.output_item.added","output_index":1,"item":{"type":"function_call","id":"fc_s1","call_id":"call_s1","name":"get_weather","arguments":"","status":"in_progress"}}

event: response.function_call_arguments.delta
data: {"type":"response.function_call_arguments.delta","item_id":"fc_s1","output_index":1,"delta":"{\\"city\\":"}

event: response.function_call_arguments.delta
data: {"type":"response.function_call_arguments.delta","item_id":"fc_s1","output_index":1,"delta":"\\"上海\\"}"}

event: response.function_call_arguments.done
data: {"type":"response.function_call_arguments.done","item_id":"fc_s1","output_index":1,"arguments":"{\\"city\\":\\"上海\\"}"}

event: response.output_item.done
data: {"type":"response.output_item.done","output_index":1,"item":{"type":"function_call","id":"fc_s1","call_id":"call_s1","name":"get_weather","arguments":"{\\"city\\":\\"上海\\"}","status":"completed"}}

event: response.completed
data: {"type":"response.completed","response":{"id":"resp_s1","status":"completed","output":[{"type":"message","id":"msg_s1","role":"assistant","status":"completed","content":[{"type":"output_text","text":"你好","annotations":[]}]},{"type":"function_call","id":"fc_s1","call_id":"call_s1","name":"get_weather","arguments":"{\\"city\\":\\"上海\\"}","status":"completed"}],"usage":{"input_tokens":50,"output_tokens":12,"input_tokens_details":{"cached_tokens":0},"output_tokens_details":{"reasoning_tokens":0}}}}

data: [DONE]

"""


async def test_stream_assembles_text_and_tool_call() -> None:
    handler, seen = sse_handler(STREAM_TOOL_CALL)
    calls: ResultCalls = []
    provider = make_provider(handler, on_result=lambda s, e: calls.append((s, e)))

    events = [e async for e in provider.respond_stream(request_of())]

    assert json.loads(seen[0].content)["stream"] is True
    assert seen[0].headers["accept"] == "text/event-stream"
    assert events[0] == TextDelta(text="你")
    assert events[1] == TextDelta(text="好")
    assert isinstance(events[2], ItemDone)
    assert events[2].item == TextOutput(text="你好", raw=events[2].item.raw)
    assert isinstance(events[3], ItemDone)
    call = events[3].item
    assert isinstance(call, FunctionCall)
    assert call.call_id == "call_s1"
    assert call.parsed_arguments == {"city": "上海"}
    assert call.parse_error is None
    assert len(events) == 5
    done = events[-1]
    assert isinstance(done, Completed)
    assert done.response.response_id == "resp_s1"
    assert done.response.status == "completed"
    assert done.response.usage == Usage(input_tokens=50, output_tokens=12)
    assert [type(i) for i in done.response.items] == [TextOutput, FunctionCall]
    assert calls == [(200, None)]


async def test_stream_falls_back_to_streamed_items_when_completed_has_no_output() -> None:
    text = """\
data: {"type":"response.output_item.done","item":{"type":"function_call","call_id":"c9","name":"ping","arguments":"{}"}}

data: {"type":"response.completed","response":{"id":"resp_fb","status":"completed","output":[]}}

"""
    handler, _ = sse_handler(text)

    events = [e async for e in make_provider(handler).respond_stream(request_of())]

    done = events[-1]
    assert isinstance(done, Completed)
    (call,) = done.response.items
    assert isinstance(call, FunctionCall)
    assert call.call_id == "c9"


async def test_stream_incomplete_is_completed_with_status() -> None:
    text = """\
data: {"type":"response.output_text.delta","delta":"半"}

data: {"type":"response.incomplete","response":{"id":"resp_i","status":"incomplete","incomplete_details":{"reason":"max_output_tokens"},"output":[]}}

"""
    handler, _ = sse_handler(text)

    events = [e async for e in make_provider(handler).respond_stream(request_of())]

    done = events[-1]
    assert isinstance(done, Completed)
    assert done.response.status == "incomplete"
    assert done.response.incomplete_reason == "max_output_tokens"


async def test_completed_event_without_trailing_blank_line_still_counts() -> None:
    text = (
        "event: response.completed\n"
        'data: {"type":"response.completed","response":{"id":"resp_eof","status":"completed",'
        '"output":[]}}'
    )
    handler, _ = sse_handler(text)

    events = [e async for e in make_provider(handler).respond_stream(request_of())]

    assert isinstance(events[-1], Completed)


async def test_truncated_stream_is_not_success() -> None:
    text = """\
data: {"type":"response.output_text.delta","delta":"你"}

data: {"type":"response.output_item.done","item":{"type":"function_call","call_id":"cx","name":"reply","arguments":"{\\"text\\":"}}

"""
    handler, _ = sse_handler(text)
    calls: ResultCalls = []
    provider = make_provider(handler, on_result=lambda s, e: calls.append((s, e)))
    seen: list[object] = []

    with pytest.raises(UpstreamUnavailable, match="before response.completed") as info:
        async for event in provider.respond_stream(request_of()):
            seen.append(event)

    assert seen[0] == TextDelta(text="你")
    assert all(not isinstance(e, Completed) for e in seen)
    assert calls == [(200, info.value)]


async def test_stream_done_marker_without_completed_is_not_success() -> None:
    handler, _ = sse_handler("data: [DONE]\n\n")

    with pytest.raises(UpstreamUnavailable, match="before response.completed"):
        _ = [e async for e in make_provider(handler).respond_stream(request_of())]


async def test_stream_response_failed_raises_with_scrubbed_detail() -> None:
    text = f"""\
data: {{"type":"response.failed","response":{{"id":"resp_f","status":"failed","error":{{"code":"server_error","message":"boom {KEY}"}}}}}}

"""
    handler, _ = sse_handler(text)
    calls: ResultCalls = []
    provider = make_provider(handler, on_result=lambda s, e: calls.append((s, e)))

    with pytest.raises(UpstreamUnavailable, match="boom") as info:
        _ = [e async for e in provider.respond_stream(request_of())]

    assert "server_error" in str(info.value)
    assert KEY not in str(info.value)
    assert KEY not in repr(calls)
    assert calls[0][1] is info.value


async def test_stream_http_error_raises_before_events() -> None:
    handler, _ = json_handler({"error": "nope"}, status=429, headers={"retry-after": "3"})

    seen: list[object] = []
    with pytest.raises(RateLimited) as info:
        async for event in make_provider(handler).respond_stream(request_of()):
            seen.append(event)

    assert seen == []
    assert info.value.retry_after == 3.0


async def test_stream_malformed_event_is_provider_error() -> None:
    handler, _ = sse_handler("data: {not json}\n\n")

    with pytest.raises(ProviderError, match="malformed"):
        _ = [e async for e in make_provider(handler).respond_stream(request_of())]


async def test_stream_timeout_mid_body_is_unavailable() -> None:
    class ExplodingStream(httpx.AsyncByteStream):
        async def __aiter__(self):  # type: ignore[override]
            yield b'data: {"type":"response.output_text.delta","delta":"x"}\n\n'
            raise httpx.ReadTimeout("stalled")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, stream=ExplodingStream(), headers={"content-type": "text/event-stream"}
        )

    with pytest.raises(UpstreamUnavailable, match="timed out"):
        _ = [e async for e in make_provider(handler).respond_stream(request_of())]
