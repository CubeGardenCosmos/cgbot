"""Sandbox search HTTP boundary tests, without network backends."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from search_fakes import FakeEngine, hit

from cgbot.coding.searchbridge import MAX_BODY_BYTES, search_routes
from cgbot.config.settings import SearchConfig
from cgbot.search.engines import EngineError
from cgbot.search.gemini import GeminiSearchClient, format_answer
from cgbot.search.models import E_TIMEOUT
from cgbot.search.service import SearchService
from cgbot.search.tools import format_outcome

AUTH_HEADER = {"Authorization": "Bearer sandbox-test"}
PATHS = ("/bridge/search", "/bridge/gemini-search")


class _Auth:
    def token_for(self, user_id: str) -> str:
        return "sandbox-test"

    def resolve(self, token: str) -> str | None:
        return "alice" if token == "sandbox-test" else None


@asynccontextmanager
async def _serve(
    search: SearchService | None = None, gemini: GeminiSearchClient | None = None
) -> AsyncIterator[TestClient]:
    app = web.Application()
    app.add_routes(search_routes(search, gemini, _Auth()))
    async with TestClient(TestServer(app)) as client:
        yield client


def _web_service() -> tuple[SearchService, FakeEngine]:
    bing = FakeEngine("bing", [hit("Example", "https://example.test", "a snippet")])
    return SearchService(
        SearchConfig(engines=("bing", "baidu"), default_engine="", max_count=10),
        engines={"bing": bing, "baidu": FakeEngine("baidu", error=EngineError(E_TIMEOUT))},
    ), bing


def _gemini(response: dict[str, object], calls: list[httpx.Request]) -> GeminiSearchClient:
    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=response)

    return GeminiSearchClient(
        "http://relay.test/search",
        "bot-only-test-token",
        "gemini-test",
        http_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("authorization", [None, "Bearer wrong", "Basic sandbox-test"])
async def test_searchbridge_requires_resolved_bearer(path: str, authorization: str | None) -> None:
    async with _serve() as client:
        headers = {} if authorization is None else {"Authorization": authorization}
        response = await client.post(path, json={"query": "query"}, headers=headers)
        assert response.status == 401
        assert await response.json() == {"error": "unauthorized", "message": "Unauthorized"}


@pytest.mark.parametrize("path", PATHS)
async def test_searchbridge_disabled_backend(path: str) -> None:
    async with _serve() as client:
        response = await client.post(path, json={"query": "query"}, headers=AUTH_HEADER)
        assert response.status == 404
        assert (await response.json())["error"] == "E_DISABLED"


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        (path, payload)
        for path in PATHS
        for payload in ([], {}, {"query": " "}, {"query": 1}, {"query": "ok", "user_id": "bob"})
    ]
    + [
        ("/bridge/search", {"query": "q" * 201}),
        ("/bridge/search", {"query": "ok", "count": True}),
        ("/bridge/search", {"query": "ok", "count": 0}),
        ("/bridge/search", {"query": "ok", "count": 11}),
        ("/bridge/search", {"query": "ok", "count": 1.5}),
        ("/bridge/search", {"query": "ok", "engine": []}),
        ("/bridge/search", {"query": "ok", "freshness": []}),
        ("/bridge/search", {"query": "ok", "freshness": "hour"}),
        ("/bridge/gemini-search", {"query": "q" * 301}),
        ("/bridge/gemini-search", {"query": "a\nb"}),
        ("/bridge/gemini-search", {"query": "ok", "recency": {}}),
        ("/bridge/gemini-search", {"query": "ok", "recency": "hour"}),
    ],
)
async def test_searchbridge_validates_before_backend(path: str, payload: object) -> None:
    search, bing = _web_service()
    calls: list[httpx.Request] = []
    gemini = _gemini({"ok": True, "answer": "Answer"}, calls)
    async with _serve(search, gemini) as client:
        response = await client.post(path, json=payload, headers=AUTH_HEADER)
        assert response.status == 400
        assert (await response.json())["error"] == "invalid_request"
    assert not calls
    assert not bing.queries


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize(
    ("data", "content_type", "status"),
    [
        (b"{broken", "application/json", 400),
        (b"{}", "text/plain", 415),
        (b"x" * (MAX_BODY_BYTES + 1), "application/json", 413),
    ],
)
async def test_searchbridge_body_boundary(
    path: str, data: bytes, content_type: str, status: int
) -> None:
    search, bing = _web_service()
    calls: list[httpx.Request] = []
    async with _serve(search, _gemini({"ok": True, "answer": "Answer"}, calls)) as client:
        response = await client.post(
            path, data=data, headers={**AUTH_HEADER, "Content-Type": content_type}
        )
        assert response.status == status
        assert (await response.json())["error"] == "invalid_request"
    assert not calls
    assert not bing.queries


async def test_searchbridge_web_partial_failure_matches_tool_and_hides_query_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    search, bing = _web_service()
    query = "private-search-canary"
    caplog.set_level(logging.WARNING)
    expected = await search.search(query, count=3, freshness="week")
    async with _serve(search) as client:
        response = await client.post(
            PATHS[0], json={"query": query, "count": 3, "freshness": "week"}, headers=AUTH_HEADER
        )
        assert response.status == 200
        assert response.content_type == "text/plain"
        assert await response.text() == format_outcome(query, expected)
    assert bing.queries[-1] == (query, 3, "week")
    assert "百度" in format_outcome(query, expected)
    assert query not in caplog.text


@pytest.mark.parametrize("engine", ["unknown", "bilibili"])
async def test_searchbridge_web_invalid_or_disabled_engine(engine: str) -> None:
    search, bing = _web_service()
    async with _serve(search) as client:
        response = await client.post(
            PATHS[0], json={"query": "query", "engine": engine}, headers=AUTH_HEADER
        )
        assert response.status == 400
        assert (await response.json())["error"] in {"E_UNKNOWN_ENGINE", "E_ENGINE_DISABLED"}
    assert not bing.queries


async def test_searchbridge_gemini_formats_answer_and_forwards_recency() -> None:
    calls: list[httpx.Request] = []
    gemini = _gemini(
        {
            "ok": True,
            "answer": "Grounded answer",
            "sources": [{"title": "Reference", "url": "https://example.test"}],
        },
        calls,
    )
    expected = await gemini.search("query", "month")
    async with _serve(gemini=gemini) as client:
        response = await client.post(
            PATHS[1], json={"query": "query", "recency": "month"}, headers=AUTH_HEADER
        )
        assert response.status == 200
        assert response.content_type == "text/plain"
        assert await response.text() == format_answer(expected)
    assert b'"recency":"month"' in calls[-1].content
    assert calls[-1].headers["Authorization"] == "Bearer bot-only-test-token"


async def test_searchbridge_gemini_refusal_is_stable_without_query_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls: list[httpx.Request] = []
    query = "private-refusal-canary"
    gemini = _gemini({"ok": False, "code": "E_REFUSED", "message": query}, calls)
    caplog.set_level(logging.WARNING)
    async with _serve(gemini=gemini) as client:
        response = await client.post(PATHS[1], json={"query": query}, headers=AUTH_HEADER)
        assert response.status == 422
        body = await response.json()
        assert body["error"] == "E_REFUSED"
        assert "不要改写问题后重试" in body["message"]
        assert query not in str(body)
    assert query not in caplog.text


@pytest.mark.parametrize("path", PATHS)
async def test_searchbridge_chunked_body_limit(path: str) -> None:
    async def chunks() -> AsyncIterator[bytes]:
        yield b"x" * (MAX_BODY_BYTES // 2)
        yield b"x" * (MAX_BODY_BYTES // 2 + 1)

    search, bing = _web_service()
    calls: list[httpx.Request] = []
    async with _serve(search, _gemini({"ok": True, "answer": "Answer"}, calls)) as client:
        response = await client.post(
            path, data=chunks(), headers={**AUTH_HEADER, "Content-Type": "application/json"}
        )
        assert response.status == 413
        assert (await response.json())["error"] == "invalid_request"
    assert not bing.queries
    assert not calls


async def test_searchbridge_all_web_engines_failed() -> None:
    search = SearchService(
        SearchConfig(engines=("bing",)),
        engines={"bing": FakeEngine("bing", error=EngineError(E_TIMEOUT))},
    )
    async with _serve(search) as client:
        response = await client.post(PATHS[0], json={"query": "query"}, headers=AUTH_HEADER)
        assert response.status == 502
        body = await response.json()
        assert body["error"] == "E_ALL_FAILED"
        assert "所有搜索引擎均未返回结果" in body["message"]


@pytest.mark.parametrize(
    ("code", "status"), [("E_TIMEOUT", 504), ("E_NETWORK", 502), ("E_BAD_REQUEST", 400)]
)
async def test_searchbridge_gemini_error_mapping(code: str, status: int) -> None:
    calls: list[httpx.Request] = []
    async with _serve(gemini=_gemini({"ok": False, "code": code}, calls)) as client:
        response = await client.post(PATHS[1], json={"query": "query"}, headers=AUTH_HEADER)
        assert response.status == status
        assert (await response.json())["error"] == code
