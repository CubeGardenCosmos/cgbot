"""Chat Completions adapter (R1.5, wire_api = "chat"): mapping, errors, streaming, selection."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from cgbot.config.settings import ConfigError, ProviderConfig, ReasoningTiers, Secret
from cgbot.provider import (
    BadRequest,
    ChatCompletionsProvider,
    Completed,
    FunctionCall,
    ItemDone,
    ModelRequest,
    ProviderError,
    RateLimited,
    ResponsesProvider,
    TextDelta,
    TextOutput,
    UpstreamUnavailable,
    Usage,
    build_provider,
    function_call_output,
    user_message,
)
from cgbot.provider.chat import chat_tool

KEY = "sk-test-SECRET-7c21d4"
BASE = "https://upstream.example/v1/"

Handler = Callable[[httpx.Request], httpx.Response]
ResultCalls = list[tuple[int | None, Exception | None]]

TEXT_RESPONSE: dict[str, Any] = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "你好"},
            "finish_reason": "stop",
        }
    ],
    "usage": {
        "prompt_tokens": 120,
        "completion_tokens": 30,
        "prompt_tokens_details": {"cached_tokens": 64},
        "completion_tokens_details": {"reasoning_tokens": 20},
    },
}

TOOL_RESPONSE: dict[str, Any] = {
    "id": "chatcmpl-2",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_w1",
                        "type": "function",
                        "function": {"name": "weather", "arguments": '{"city": "上海"}'},
                    }
                ],
            },
            "finish_reason": "tool_calls",
        }
    ],
    "usage": {"prompt_tokens": 50, "completion_tokens": 12},
}

WEATHER_TOOL = {
    "type": "function",
    "name": "weather",
    "description": "查天气",
    "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
    "strict": False,
}


def make_provider(handler: Handler, on_result: Any = None) -> ChatCompletionsProvider:
    return ChatCompletionsProvider(
        base_url=BASE,
        api_key=KEY,
        model="chat-model",
        context_window=64000,
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        on_result=on_result,
        retry_attempts=0,
    )


def json_handler(
    payload: dict[str, Any], status: int = 200, seen: list[httpx.Request] | None = None
) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, json=payload)

    return handler


def sse_handler(lines: list[str], seen: list[httpx.Request] | None = None) -> Handler:
    body = "".join(f"data: {line}\n\n" for line in lines).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    return handler


def request_of(**overrides: Any) -> ModelRequest:
    fields: dict[str, Any] = {"instructions": "你是助手。", "input": [user_message("你好")]}
    fields.update(overrides)
    return ModelRequest(**fields)


def sent_body(seen: list[httpx.Request]) -> dict[str, Any]:
    return json.loads(seen[0].content)


# --- request mapping -------------------------------------------------------------------------


async def test_text_request_shape_and_text_response() -> None:
    seen: list[httpx.Request] = []
    calls: ResultCalls = []
    provider = make_provider(
        json_handler(TEXT_RESPONSE, seen=seen), on_result=lambda s, e: calls.append((s, e))
    )

    result = await provider.respond(request_of(reasoning_effort="high", max_output_tokens=256))

    assert str(seen[0].url) == "https://upstream.example/v1/chat/completions"
    assert seen[0].headers["authorization"] == f"Bearer {KEY}"
    body = sent_body(seen)
    assert body == {
        "model": "chat-model",
        "messages": [
            {"role": "system", "content": "你是助手。"},
            {"role": "user", "content": "你好"},
        ],
        "stream": False,
        "reasoning_effort": "high",
        "max_tokens": 256,
    }
    assert result.response_id == "chatcmpl-1"
    assert result.status == "completed"
    assert result.incomplete_reason is None
    assert result.usage == Usage(input_tokens=120, output_tokens=30, cached=64, reasoning=20)
    assert result.items == (
        TextOutput(
            text="你好",
            raw={
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "你好"}],
            },
        ),
    )
    assert calls == [(200, None)]


async def test_tool_round_trip_request_is_valid_chat_sequence() -> None:
    seen: list[httpx.Request] = []
    provider = make_provider(json_handler(TEXT_RESPONSE, seen=seen))
    history: list[dict[str, Any]] = [
        user_message("查上海天气"),
        {"type": "reasoning", "summary": [], "encrypted_content": "ENC"},
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "好的"}],
        },
        {
            "type": "function_call",
            "call_id": "call_w1",
            "name": "weather",
            "arguments": '{"city": "上海"}',
        },
        {"type": "function_call", "call_id": "call_orphan", "name": "x", "arguments": "{}"},
        function_call_output("call_w1", "晴"),
        function_call_output("call_gone", "孤儿输出"),
        user_message("那明天呢"),
    ]

    await provider.respond(request_of(input=history, tools=[WEATHER_TOOL]))

    body = sent_body(seen)
    assert body["messages"] == [
        {"role": "system", "content": "你是助手。"},
        {"role": "user", "content": "查上海天气"},
        {
            "role": "assistant",
            "content": "好的",
            "tool_calls": [
                {
                    "id": "call_w1",
                    "type": "function",
                    "function": {"name": "weather", "arguments": '{"city": "上海"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_w1", "content": "晴"},
        {"role": "user", "content": "那明天呢"},
    ]
    assert body["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "weather",
                "description": "查天气",
                "parameters": WEATHER_TOOL["parameters"],
                "strict": False,
            },
        }
    ]


def test_chat_tool_rejects_non_function_tools() -> None:
    with pytest.raises(ValueError, match="function tools only"):
        chat_tool({"type": "web_search"})


async def test_empty_instructions_and_unknown_items_are_skipped() -> None:
    seen: list[httpx.Request] = []
    provider = make_provider(json_handler(TEXT_RESPONSE, seen=seen))

    await provider.respond(
        request_of(
            instructions="",
            input=[
                {"type": "web_search_call", "id": "ws_1"},
                {"type": "message", "role": "developer", "content": "规则"},
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_file", "file_id": "f1"},
                        {"type": "input_text", "text": "看图"},
                    ],
                },
            ],
        )
    )

    assert sent_body(seen)["messages"] == [
        {"role": "system", "content": "规则"},
        {"role": "user", "content": "看图"},
    ]


async def test_user_input_image_parts_become_image_url_parts_in_order() -> None:
    seen: list[httpx.Request] = []
    provider = make_provider(json_handler(TEXT_RESPONSE, seen=seen))
    data_url = "data:image/jpeg;base64,AAAA"

    await provider.respond(
        request_of(
            instructions="",
            input=[
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "看图[图片]"},
                        {"type": "input_image", "image_url": data_url},
                        {"type": "input_image", "image_url": ""},
                    ],
                },
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "input_image", "image_url": data_url}],
                },
            ],
        )
    )

    assert sent_body(seen)["messages"] == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "看图[图片]"},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }
    ]


# --- non-streaming results --------------------------------------------------------------------


async def test_tool_call_response_maps_to_function_call() -> None:
    provider = make_provider(json_handler(TOOL_RESPONSE))

    result = await provider.respond(request_of(tools=[WEATHER_TOOL]))

    assert result.status == "completed"
    assert result.items == (
        FunctionCall(
            call_id="call_w1",
            name="weather",
            arguments='{"city": "上海"}',
            parsed_arguments={"city": "上海"},
            parse_error=None,
            raw={
                "type": "function_call",
                "call_id": "call_w1",
                "name": "weather",
                "arguments": '{"city": "上海"}',
            },
        ),
    )


async def test_invalid_tool_arguments_are_kept_with_error_position() -> None:
    payload = json.loads(json.dumps(TOOL_RESPONSE))
    payload["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = '{"city": '
    provider = make_provider(json_handler(payload))

    result = await provider.respond(request_of())

    call = result.items[0]
    assert isinstance(call, FunctionCall)
    assert call.parsed_arguments is None
    assert call.parse_error is not None and call.parse_error.startswith("invalid JSON at char")
    assert call.arguments == '{"city": '


@pytest.mark.parametrize(
    ("reason", "expected"),
    [("length", "max_output_tokens"), ("content_filter", "content_filter"), ("weird", "weird")],
)
async def test_non_stop_finish_reasons_are_incomplete(reason: str, expected: str) -> None:
    payload = json.loads(json.dumps(TEXT_RESPONSE))
    payload["choices"][0]["finish_reason"] = reason
    provider = make_provider(json_handler(payload))

    result = await provider.respond(request_of())

    assert result.status == "incomplete"
    assert result.incomplete_reason == expected


async def test_missing_finish_reason_is_malformed() -> None:
    payload = json.loads(json.dumps(TEXT_RESPONSE))
    del payload["choices"][0]["finish_reason"]
    provider = make_provider(json_handler(payload))

    with pytest.raises(ProviderError, match="no finish_reason"):
        await provider.respond(request_of())


# --- errors -----------------------------------------------------------------------------------


async def test_429_is_rate_limited_with_retry_after() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "7"}, text=f"slow down {KEY}")

    calls: ResultCalls = []
    provider = make_provider(handler, on_result=lambda s, e: calls.append((s, e)))

    with pytest.raises(RateLimited) as info:
        await provider.respond(request_of())

    assert info.value.retry_after == 7.0
    assert KEY not in str(info.value)
    assert calls[0][0] == 429


async def test_5xx_is_upstream_unavailable_and_4xx_is_bad_request() -> None:
    provider_5xx = make_provider(json_handler({"error": "boom"}, status=503))
    with pytest.raises(UpstreamUnavailable):
        await provider_5xx.respond(request_of())

    provider_4xx = make_provider(json_handler({"error": "bad"}, status=400))
    with pytest.raises(BadRequest):
        await provider_4xx.respond(request_of())


async def test_timeout_is_mapped_without_leaking_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(f"slow {KEY}", request=request)

    provider = make_provider(handler)

    with pytest.raises(UpstreamUnavailable, match="timed out") as info:
        await provider.respond(request_of())

    assert KEY not in str(info.value)


def test_repr_hides_key() -> None:
    provider = make_provider(json_handler(TEXT_RESPONSE))

    text = repr(provider)

    assert KEY not in text
    assert "ChatCompletionsProvider" in text
    assert "api_key='***'" in text


# --- streaming --------------------------------------------------------------------------------


async def test_stream_assembles_text_and_tool_call_with_usage_chunk() -> None:
    seen: list[httpx.Request] = []
    lines = [
        json.dumps(
            {
                "id": "c_s",
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": "你"}}],
            }
        ),
        json.dumps({"id": "c_s", "choices": [{"index": 0, "delta": {"content": "好"}}]}),
        json.dumps(
            {
                "id": "c_s",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_s1",
                                    "type": "function",
                                    "function": {"name": "weather", "arguments": '{"city": '},
                                }
                            ]
                        },
                    }
                ],
            }
        ),
        json.dumps(
            {
                "id": "c_s",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [{"index": 0, "function": {"arguments": '"上海"}'}}]
                        },
                    }
                ],
            }
        ),
        json.dumps(
            {"id": "c_s", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}
        ),
        json.dumps(
            {"id": "c_s", "choices": [], "usage": {"prompt_tokens": 50, "completion_tokens": 12}}
        ),
        "[DONE]",
    ]
    calls: ResultCalls = []
    provider = make_provider(
        sse_handler(lines, seen=seen), on_result=lambda s, e: calls.append((s, e))
    )

    events = [e async for e in provider.respond_stream(request_of())]

    body = sent_body(seen)
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
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
    done = events[-1]
    assert isinstance(done, Completed)
    assert done.response.response_id == "c_s"
    assert done.response.status == "completed"
    assert done.response.usage == Usage(input_tokens=50, output_tokens=12)
    assert len(events) == 5
    assert calls == [(200, None)]


async def test_stream_without_finish_reason_is_unavailable() -> None:
    lines = [
        json.dumps({"id": "c_t", "choices": [{"index": 0, "delta": {"content": "半"}}]}),
        "[DONE]",
    ]
    calls: ResultCalls = []
    provider = make_provider(sse_handler(lines), on_result=lambda s, e: calls.append((s, e)))

    with pytest.raises(UpstreamUnavailable, match="finish_reason"):
        _ = [e async for e in provider.respond_stream(request_of())]

    assert calls[0][0] == 200 and isinstance(calls[0][1], UpstreamUnavailable)


async def test_stream_error_event_is_unavailable_and_scrubbed() -> None:
    lines = [json.dumps({"error": {"message": f"quota {KEY}", "code": "rate"}})]
    provider = make_provider(sse_handler(lines))

    with pytest.raises(UpstreamUnavailable, match="upstream stream failed") as info:
        _ = [e async for e in provider.respond_stream(request_of())]

    assert KEY not in str(info.value)


# --- selection --------------------------------------------------------------------------------


def _config(wire_api: str) -> ProviderConfig:
    return ProviderConfig(
        base_url=BASE,
        model="m",
        context_window=1000,
        api_key=Secret(KEY),
        reasoning=ReasoningTiers(),
        wire_api=wire_api,  # type: ignore[arg-type]
    )


def test_build_provider_defaults_to_responses_and_selects_chat() -> None:
    responses = build_provider(_config("responses"))
    chat = build_provider(_config("chat"))

    assert isinstance(responses, ResponsesProvider)
    assert isinstance(chat, ChatCompletionsProvider)
    assert chat.name == "m"
    assert chat.context_window == 1000


def test_build_provider_rejects_unknown_wire_api() -> None:
    with pytest.raises(ConfigError, match="provider.wire_api"):
        build_provider(_config("grpc"))
