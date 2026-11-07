"""Gemini 原生适配器(wire_api = "gemini"):请求映射、解析、流式、错误。"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from cgbot.provider import (
    BadRequest,
    Completed,
    FunctionCall,
    ItemDone,
    ModelRequest,
    ProviderError,
    RateLimited,
    TextDelta,
    TextOutput,
    UpstreamUnavailable,
    Usage,
    function_call_output,
    user_message,
)
from cgbot.provider.gemini import GeminiProvider

KEY = "AIza-test-SECRET-9f3a"
BASE = "https://upstream.example/v1beta/"
SIG = "skip_thought_signature_validator"

Handler = Callable[[httpx.Request], httpx.Response]
ResultCalls = list[tuple[int | None, Exception | None]]

TEXT_RESPONSE: dict[str, Any] = {
    "responseId": "resp-1",
    "candidates": [
        {
            "content": {
                "role": "model",
                "parts": [
                    {"text": "先想想", "thought": True},
                    {"text": "你好", "thoughtSignature": "SIGNATURE"},
                ],
            },
            "finishReason": "STOP",
        }
    ],
    "usageMetadata": {
        "promptTokenCount": 120,
        "candidatesTokenCount": 10,
        "thoughtsTokenCount": 20,
        "cachedContentTokenCount": 64,
        "totalTokenCount": 150,
    },
}

WEATHER_TOOL = {
    "type": "function",
    "name": "weather",
    "description": "查天气",
    "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
    "strict": False,
}


def make_provider(handler: Handler, on_result: Any = None) -> GeminiProvider:
    return GeminiProvider(
        base_url=BASE,
        api_key=KEY,
        model="gem-model",
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


def sse_handler(chunks: list[dict[str, Any]], seen: list[httpx.Request] | None = None) -> Handler:
    body = "".join(f"data: {json.dumps(chunk)}\r\n\r\n" for chunk in chunks).encode()

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


def candidate(parts: list[dict[str, Any]], finish: str | None = "STOP") -> dict[str, Any]:
    cand: dict[str, Any] = {"content": {"role": "model", "parts": parts}}
    if finish is not None:
        cand["finishReason"] = finish
    return {"candidates": [cand]}


# --- request ---------------------------------------------------------------------------------


async def test_url_headers_and_basic_body() -> None:
    seen: list[httpx.Request] = []
    calls: ResultCalls = []
    provider = make_provider(
        json_handler(TEXT_RESPONSE, seen=seen), on_result=lambda s, e: calls.append((s, e))
    )

    result = await provider.respond(request_of(max_output_tokens=256))

    assert str(seen[0].url) == "https://upstream.example/v1beta/models/gem-model:generateContent"
    assert seen[0].headers["x-goog-api-key"] == KEY
    assert "authorization" not in seen[0].headers
    assert KEY not in str(seen[0].url)
    assert sent_body(seen) == {
        "contents": [{"role": "user", "parts": [{"text": "你好"}]}],
        "systemInstruction": {"parts": [{"text": "你是助手。"}]},
        "generationConfig": {"maxOutputTokens": 256},
    }
    assert result.status == "completed"
    assert calls == [(200, None)]


async def test_stream_url() -> None:
    seen: list[httpx.Request] = []
    provider = make_provider(sse_handler([candidate([{"text": "x"}])], seen))

    [event async for event in provider.respond_stream(request_of())]

    assert str(seen[0].url) == (
        "https://upstream.example/v1beta/models/gem-model:streamGenerateContent?alt=sse"
    )
    assert seen[0].headers["x-goog-api-key"] == KEY
    assert KEY not in str(seen[0].url)


async def test_empty_instructions_omit_system_instruction() -> None:
    seen: list[httpx.Request] = []
    provider = make_provider(json_handler(TEXT_RESPONSE, seen=seen))

    await provider.respond(request_of(instructions=""))

    assert "systemInstruction" not in sent_body(seen)
    assert "generationConfig" not in sent_body(seen)


async def test_images_become_inline_data_and_roles_merge() -> None:
    seen: list[httpx.Request] = []
    provider = make_provider(json_handler(TEXT_RESPONSE, seen=seen))
    history: list[dict[str, Any]] = [
        {
            "type": "message",
            "role": "user",
            "content": [
                {"type": "input_text", "text": "看图"},
                {"type": "input_image", "image_url": "data:image/png;base64,QUJD"},
                {"type": "input_image", "image_url": "https://example.com/x.png"},
            ],
        },
        {
            "type": "message",
            "role": "developer",
            "content": [{"type": "input_text", "text": "注意"}],
        },
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "好的"}],
        },
        {"type": "reasoning", "summary": [], "encrypted_content": "ENC"},
        user_message("继续"),
    ]

    await provider.respond(request_of(input=history))

    assert sent_body(seen)["contents"] == [
        {
            "role": "user",
            "parts": [
                {"text": "看图"},
                {"inlineData": {"mimeType": "image/png", "data": "QUJD"}},
                {"text": "注意"},
            ],
        },
        {"role": "model", "parts": [{"text": "好的"}]},
        {"role": "user", "parts": [{"text": "继续"}]},
    ]


async def test_function_calls_group_with_signatures_and_responses() -> None:
    seen: list[httpx.Request] = []
    provider = make_provider(json_handler(TEXT_RESPONSE, seen=seen))
    history: list[dict[str, Any]] = [
        user_message("查天气"),
        {
            "type": "function_call",
            "call_id": "call_a",
            "name": "weather",
            "arguments": '{"city": "上海"}',
        },
        {"type": "function_call", "call_id": "call_b", "name": "time", "arguments": ""},
        {"type": "function_call", "call_id": "call_orphan", "name": "x", "arguments": "{}"},
        function_call_output("call_b", "12:00"),
        function_call_output("call_a", "晴"),
        function_call_output("call_gone", "孤儿"),
        user_message("明天呢"),
    ]

    await provider.respond(request_of(input=history, tools=[WEATHER_TOOL]))

    assert sent_body(seen)["contents"] == [
        {"role": "user", "parts": [{"text": "查天气"}]},
        {
            "role": "model",
            "parts": [
                {
                    "functionCall": {"name": "weather", "args": {"city": "上海"}},
                    "thoughtSignature": SIG,
                },
                {"functionCall": {"name": "time", "args": {}}, "thoughtSignature": SIG},
            ],
        },
        {
            "role": "user",
            "parts": [
                {"functionResponse": {"name": "weather", "response": {"output": "晴"}}},
                {"functionResponse": {"name": "time", "response": {"output": "12:00"}}},
                {"text": "明天呢"},
            ],
        },
    ]


async def test_tools_and_thinking_level() -> None:
    seen: list[httpx.Request] = []
    provider = make_provider(json_handler(TEXT_RESPONSE, seen=seen))

    await provider.respond(request_of(tools=[WEATHER_TOOL], reasoning_effort="xhigh"))

    body = sent_body(seen)
    assert body["tools"] == [
        {
            "functionDeclarations": [
                {
                    "name": "weather",
                    "description": "查天气",
                    "parametersJsonSchema": WEATHER_TOOL["parameters"],
                }
            ]
        }
    ]
    assert body["generationConfig"] == {"thinkingConfig": {"thinkingLevel": "high"}}


@pytest.mark.parametrize(
    ("effort", "level"),
    [
        ("none", "minimal"),
        ("minimal", "minimal"),
        ("low", "low"),
        ("medium", "medium"),
        ("high", "high"),
        ("max", "high"),
        ("bogus", None),
    ],
)
async def test_thinking_level_mapping(effort: str, level: str | None) -> None:
    seen: list[httpx.Request] = []
    provider = make_provider(json_handler(TEXT_RESPONSE, seen=seen))

    await provider.respond(request_of(reasoning_effort=effort))

    config = sent_body(seen).get("generationConfig", {})
    if level is None:
        assert "thinkingConfig" not in config
    else:
        assert config["thinkingConfig"] == {"thinkingLevel": level}


async def test_non_function_tool_is_rejected() -> None:
    provider = make_provider(json_handler(TEXT_RESPONSE))

    with pytest.raises(ValueError, match="function tools only"):
        await provider.respond(request_of(tools=[{"type": "web_search"}]))


# --- response --------------------------------------------------------------------------------


async def test_text_response_drops_thought_and_signature() -> None:
    provider = make_provider(json_handler(TEXT_RESPONSE))

    result = await provider.respond(request_of())

    assert result.response_id == "resp-1"
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
    assert "SIGNATURE" not in json.dumps([item.raw for item in result.items])


async def test_function_call_response() -> None:
    payload = candidate(
        [
            {"text": "想", "thought": True, "thoughtSignature": "S1"},
            {"text": "查一下"},
            {
                "functionCall": {"id": "fc-1", "name": "weather", "args": {"city": "上海"}},
                "thoughtSignature": "S2",
            },
            {"functionCall": {"name": "time", "args": {}}},
        ]
    )
    provider = make_provider(json_handler(payload))

    result = await provider.respond(request_of())

    text, first, second = result.items
    assert isinstance(text, TextOutput)
    assert isinstance(first, FunctionCall)
    assert first.call_id == "fc-1"
    assert first.parsed_arguments == {"city": "上海"}
    assert first.raw == {
        "type": "function_call",
        "call_id": "fc-1",
        "name": "weather",
        "arguments": '{"city": "上海"}',
    }
    assert isinstance(second, FunctionCall)
    assert second.call_id.startswith("call_")
    assert len(second.call_id) > len("call_")
    assert second.raw["call_id"] == second.call_id
    assert second.arguments == "{}"
    assert result.status == "completed"
    assert "S2" not in json.dumps([item.raw for item in result.items])


async def test_max_tokens_is_incomplete() -> None:
    provider = make_provider(json_handler(candidate([{"text": "半截"}], "MAX_TOKENS")))

    result = await provider.respond(request_of())

    assert result.status == "incomplete"
    assert result.incomplete_reason == "max_output_tokens"
    assert [item.text for item in result.items if isinstance(item, TextOutput)] == ["半截"]


async def test_other_finish_reason_is_incomplete() -> None:
    provider = make_provider(json_handler(candidate([], "MALFORMED_FUNCTION_CALL")))

    result = await provider.respond(request_of())

    assert result.status == "incomplete"
    assert result.incomplete_reason == "malformed_function_call"


@pytest.mark.parametrize(
    "payload",
    [
        candidate([], "SAFETY"),
        candidate([{"text": "x"}], "PROHIBITED_CONTENT"),
        {"promptFeedback": {"blockReason": "SAFETY"}},
    ],
)
async def test_content_block_is_bad_request(payload: dict[str, Any]) -> None:
    provider = make_provider(json_handler(payload))

    with pytest.raises(BadRequest):
        await provider.respond(request_of())


async def test_no_candidates_is_provider_error() -> None:
    provider = make_provider(json_handler({"usageMetadata": None}))

    with pytest.raises(ProviderError):
        await provider.respond(request_of())


# --- errors ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "error"),
    [(429, RateLimited), (503, UpstreamUnavailable), (500, UpstreamUnavailable), (400, BadRequest)],
)
async def test_http_error_mapping(status: int, error: type[ProviderError]) -> None:
    calls: ResultCalls = []
    body = {"error": {"status": "RESOURCE_EXHAUSTED", "message": f"bad key {KEY}"}}
    provider = make_provider(
        json_handler(body, status=status), on_result=lambda s, e: calls.append((s, e))
    )

    with pytest.raises(error) as info:
        await provider.respond(request_of())

    assert info.value.status == status
    assert KEY not in str(info.value)
    assert KEY not in info.value.body
    assert calls[0][0] == status


async def test_stream_http_error_mapping() -> None:
    provider = make_provider(json_handler({"error": {}}, status=429))

    with pytest.raises(RateLimited):
        [event async for event in provider.respond_stream(request_of())]


# --- streaming -------------------------------------------------------------------------------


async def test_stream_text_and_calls() -> None:
    chunks = [
        {**candidate([{"text": "想", "thought": True}], None), "responseId": "r-1"},
        candidate([{"text": "你"}], None),
        candidate([{"text": "好", "thoughtSignature": "S"}], None),
        {
            **candidate(
                [{"functionCall": {"id": "fc-9", "name": "weather", "args": {"city": "北京"}}}],
                "STOP",
            ),
            "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 3},
        },
    ]
    provider = make_provider(sse_handler(chunks))

    events = [event async for event in provider.respond_stream(request_of())]

    assert [e.text for e in events if isinstance(e, TextDelta)] == ["你", "好"]
    assert isinstance(events[-1], Completed)
    done = [e.item for e in events if isinstance(e, ItemDone)]
    assert [type(item) for item in done] == [TextOutput, FunctionCall]
    final = events[-1].response
    assert final.items == tuple(done)
    assert final.response_id == "r-1"
    assert final.status == "completed"
    assert final.usage == Usage(input_tokens=7, output_tokens=3)
    assert done[0].raw["content"] == [{"type": "output_text", "text": "你好"}]


async def test_stream_max_tokens_incomplete() -> None:
    provider = make_provider(
        sse_handler([candidate([{"text": "半"}], None), candidate([{"text": "截"}], "MAX_TOKENS")])
    )

    events = [event async for event in provider.respond_stream(request_of())]

    final = events[-1]
    assert isinstance(final, Completed)
    assert final.response.status == "incomplete"
    assert final.response.incomplete_reason == "max_output_tokens"


async def test_stream_without_finish_reason_fails() -> None:
    provider = make_provider(sse_handler([candidate([{"text": "半"}], None)]))

    with pytest.raises(UpstreamUnavailable):
        [event async for event in provider.respond_stream(request_of())]


async def test_stream_error_event_fails_after_deltas() -> None:
    provider = make_provider(
        sse_handler([candidate([{"text": "半"}], None), {"error": {"message": "boom"}}])
    )
    seen: list[Any] = []

    with pytest.raises(UpstreamUnavailable):
        async for event in provider.respond_stream(request_of()):
            seen.append(event)

    assert seen == [TextDelta(text="半")]


async def test_stream_safety_block_is_bad_request() -> None:
    provider = make_provider(sse_handler([candidate([], "SAFETY")]))

    with pytest.raises(BadRequest):
        [event async for event in provider.respond_stream(request_of())]
