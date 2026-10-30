"""GeminiSearchClient 与 gemini_search 工具的离线测试(httpx.MockTransport,无网络)。"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any, cast

import httpx
import pytest

from cgbot.agent.tools import ToolContext
from cgbot.search.gemini import (
    ERROR_MESSAGES,
    GeminiAnswer,
    GeminiSearchClient,
    gemini_search_tool,
)

ENDPOINT = "http://host.docker.internal:8791/search"
TOKEN = "tok-" + "x" * 40
MODEL = "gemini-2.5-flash"
QUERY_MARKER = "CLIENT-QUERY-MARKER 最新消息"


def _factory(
    handler: Callable[[httpx.Request], httpx.Response],
) -> Callable[[], httpx.AsyncClient]:
    def make() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    return make


def _json(status: int, payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(status, json=payload)


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> GeminiSearchClient:
    return GeminiSearchClient(ENDPOINT, TOKEN, MODEL, 5.0, http_factory=_factory(handler))


def _counting(
    response: Callable[[httpx.Request], httpx.Response],
) -> tuple[list[httpx.Request], Callable[[httpx.Request], httpx.Response]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return response(request)

    return seen, handler


async def test_success_parses_answer_and_sources() -> None:
    seen, handler = _counting(
        lambda _r: _json(
            200,
            {
                "ok": True,
                "model": MODEL,
                "answer": "  Python 3.14 发布了新特性。  ",
                "sources": [
                    {"title": "What's New", "url": "https://docs.python.org/3.14/"},
                    {"title": "Notes", "url": ""},
                ],
            },
        )
    )
    result = await _client(handler).search("python 3.14", recency="week")

    assert result.ok is True
    assert result.answer == "Python 3.14 发布了新特性。"
    assert result.sources[0].title == "What's New"
    assert result.sources[0].url == "https://docs.python.org/3.14/"
    assert result.sources[1].url == ""
    assert result.code is None

    [request] = seen
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert str(request.url) == ENDPOINT
    body = json.loads(request.content)
    assert body == {"query": "python 3.14", "limit": 5, "model": MODEL, "recency": "week"}


async def test_request_omits_recency_when_not_given() -> None:
    seen, handler = _counting(lambda _r: _json(200, {"ok": True, "answer": "a", "sources": []}))
    await _client(handler).search("hello")
    body = json.loads(seen[0].content)
    assert "recency" not in body


@pytest.mark.parametrize("code", sorted(ERROR_MESSAGES))
async def test_relay_error_codes_pass_through(code: str) -> None:
    status = 200 if code in {"E_REFUSED", "E_NO_RESULT"} else 500
    _, handler = _counting(lambda _r, c=code, s=status: _json(s, {"ok": False, "code": c}))
    result = await _client(handler).search("hello")
    assert result == GeminiAnswer(ok=False, code=code)


async def test_unknown_code_maps_to_e_http() -> None:
    _, handler = _counting(lambda _r: _json(500, {"ok": False, "code": "E_WHATEVER"}))
    result = await _client(handler).search("hello")
    assert result.code == "E_HTTP"


async def test_401_maps_to_e_auth_even_without_json() -> None:
    _, handler = _counting(lambda _r: httpx.Response(401, text="nope"))
    result = await _client(handler).search("hello")
    assert result.code == "E_AUTH"


async def test_non_json_error_maps_to_e_http() -> None:
    _, handler = _counting(lambda _r: httpx.Response(502, text="<html>bad gateway</html>"))
    result = await _client(handler).search("hello")
    assert result.code == "E_HTTP"


async def test_non_json_200_maps_to_e_parse() -> None:
    _, handler = _counting(lambda _r: httpx.Response(200, text="not json"))
    result = await _client(handler).search("hello")
    assert result.code == "E_PARSE"


async def test_ok_with_bad_shape_maps_to_e_parse() -> None:
    bad_payloads: list[dict[str, Any]] = [
        {"ok": True, "answer": 42, "sources": []},
        {"ok": True, "answer": "a", "sources": "not-a-list"},
        {"ok": True, "answer": "a", "sources": [{"title": 1, "url": "x"}]},
        {"ok": True, "answer": "a", "sources": ["bare"]},
    ]
    for payload in bad_payloads:
        _, handler = _counting(lambda _r, p=payload: _json(200, p))
        result = await _client(handler).search("hello")
        assert result.code == "E_PARSE", payload


async def test_ok_with_blank_answer_maps_to_e_no_result() -> None:
    _, handler = _counting(lambda _r: _json(200, {"ok": True, "answer": "  ", "sources": []}))
    result = await _client(handler).search("hello")
    assert result.code == "E_NO_RESULT"


async def test_timeout_and_network_errors() -> None:
    def timeout(_r: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=_r)

    def refused(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=_r)

    assert (await _client(timeout).search("hello")).code == "E_TIMEOUT"
    assert (await _client(refused).search("hello")).code == "E_NETWORK"


@pytest.mark.parametrize(
    "query",
    ["", "   ", "x" * 301, "line1\nline2", "tab\there", "bell\x07"],
)
async def test_invalid_query_rejected_locally(query: str) -> None:
    seen, handler = _counting(lambda _r: _json(200, {"ok": True, "answer": "a", "sources": []}))
    result = await _client(handler).search(query)
    assert result.code == "E_BAD_QUERY"
    assert seen == []


async def test_invalid_recency_rejected_locally() -> None:
    seen, handler = _counting(lambda _r: _json(200, {"ok": True, "answer": "a", "sources": []}))
    result = await _client(handler).search("hello", recency="decade")
    assert result.code == "E_BAD_REQUEST"
    assert seen == []


async def test_logs_only_codes_never_query_or_token(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="cgbot.search.gemini")
    _, handler = _counting(lambda _r: _json(500, {"ok": False, "code": "E_OMP"}))
    result = await _client(handler).search(QUERY_MARKER)
    assert result.code == "E_OMP"
    assert "E_OMP" in caplog.text
    assert QUERY_MARKER not in caplog.text
    assert TOKEN not in caplog.text


@pytest.mark.parametrize(
    ("endpoint", "token", "model", "timeout"),
    [
        ("ftp://host/search", TOKEN, MODEL, 5.0),
        ("host-only", TOKEN, MODEL, 5.0),
        (ENDPOINT, "   ", MODEL, 5.0),
        (ENDPOINT, TOKEN, "gemini; rm -rf /", 5.0),
        (ENDPOINT, TOKEN, "--model=evil", 5.0),
        (ENDPOINT, TOKEN, MODEL, 0.0),
    ],
)
def test_constructor_rejects_bad_config(
    endpoint: str, token: str, model: str, timeout: float
) -> None:
    with pytest.raises(ValueError):
        GeminiSearchClient(endpoint, token, model, timeout)


def _ctx() -> ToolContext:
    # 工具处理器不读取上下文,测试中传入占位即可。
    return cast(ToolContext, None)


async def test_tool_formats_success_for_model() -> None:
    _, handler = _counting(
        lambda _r: _json(
            200,
            {
                "ok": True,
                "answer": "答案正文",
                "sources": [
                    {"title": "来源一", "url": "https://a.example/"},
                    {"title": "", "url": ""},
                ],
            },
        )
    )
    tool = gemini_search_tool(
        ENDPOINT, TOKEN, model=MODEL, timeout=5.0, http_factory=_factory(handler)
    )
    result = await tool.handler(_ctx(), {"query": "问题"})

    assert result.is_error is False
    assert "答案正文" in result.output
    assert "1. 来源一 https://a.example/" in result.output
    assert "2. 未命名来源" in result.output
    assert TOKEN not in result.output
    assert ENDPOINT not in result.output


async def test_tool_refusal_is_polite_error() -> None:
    _, handler = _counting(lambda _r: _json(200, {"ok": False, "code": "E_REFUSED"}))
    tool = gemini_search_tool(
        ENDPOINT, TOKEN, model=MODEL, timeout=5.0, http_factory=_factory(handler)
    )
    result = await tool.handler(_ctx(), {"query": "问题"})

    assert result.is_error is True
    assert result.output.startswith("E_REFUSED:")
    assert "不要改写" in result.output


async def test_tool_error_codes_are_stable_prefixes() -> None:
    _, handler = _counting(lambda _r: _json(429, {"ok": False, "code": "E_BUSY"}))
    tool = gemini_search_tool(
        ENDPOINT, TOKEN, model=MODEL, timeout=5.0, http_factory=_factory(handler)
    )
    result = await tool.handler(_ctx(), {"query": "问题"})
    assert result.is_error is True
    assert result.output.startswith("E_BUSY:")


async def test_tool_rejects_unknown_arguments_without_request() -> None:
    seen, handler = _counting(lambda _r: _json(200, {"ok": True, "answer": "a", "sources": []}))
    tool = gemini_search_tool(
        ENDPOINT, TOKEN, model=MODEL, timeout=5.0, http_factory=_factory(handler)
    )
    result = await tool.handler(_ctx(), {"query": "问题", "model": "evil"})
    assert result.is_error is True
    assert seen == []


def test_tool_schema_is_strict() -> None:
    tool = gemini_search_tool(
        ENDPOINT, TOKEN, model=MODEL, timeout=5.0, http_factory=_factory(lambda _r: _json(200, {}))
    )
    assert tool.name == "gemini_search"
    schema = tool.parameters_schema
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["query"]
    assert schema["properties"]["query"]["maxLength"] == 300
    assert schema["properties"]["recency"]["enum"] == ["day", "week", "month", "year"]
    assert "Google" in tool.description
