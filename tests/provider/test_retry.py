"""Transient-failure retry in the shared transport: only before any response is open."""

from __future__ import annotations

import logging
from collections.abc import Callable

import httpx
import pytest

from cgbot.config.settings import ProviderConfig, ReasoningTiers, Secret
from cgbot.provider import (
    BadRequest,
    RateLimited,
    ResponsesProvider,
    TextDelta,
    UpstreamUnavailable,
    build_provider,
    user_message,
)
from cgbot.provider.types import ModelRequest

OK_BODY = {"id": "resp_1", "object": "response", "status": "completed", "output": []}
Handler = Callable[[httpx.Request], httpx.Response]


class Sleeps:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def make(handler: Handler, sleeps: Sleeps, **kw: object) -> ResponsesProvider:
    return ResponsesProvider(
        "https://up.example/v1",
        "sk-test",
        "m",
        1000,
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        sleep=sleeps,
        **kw,  # type: ignore[arg-type]
    )


def req() -> ModelRequest:
    return ModelRequest(instructions="x", input=[user_message("hi")])


async def test_connect_error_then_success_is_one_response(
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls: list[int] = []
    results: list[tuple[int | None, Exception | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, json=OK_BODY)

    sleeps = Sleeps()
    provider = make(handler, sleeps)
    provider.on_result = lambda s, e: results.append((s, e))
    with caplog.at_level(logging.WARNING, logger="cgbot.provider.transport"):
        response = await provider.respond(req())

    assert response.status == "completed"
    assert len(calls) == 2
    assert sleeps.delays == [2.0]
    assert results == [(200, None)]
    retry_logs = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(retry_logs) == 1 and "UpstreamUnavailable" in retry_logs[0].getMessage()
    assert "ConnectError" in retry_logs[0].getMessage()


async def test_persistent_failure_stops_after_attempts_plus_one() -> None:
    calls: list[int] = []
    results: list[tuple[int | None, Exception | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503, text="down")

    sleeps = Sleeps()
    provider = make(handler, sleeps)
    provider.on_result = lambda s, e: results.append((s, e))
    with pytest.raises(UpstreamUnavailable):
        await provider.respond(req())

    assert len(calls) == 3
    assert sleeps.delays == [2.0, 5.0]
    assert len(results) == 1 and results[0][0] == 503


async def test_retry_attempts_zero_never_retries() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ConnectTimeout("slow", request=request)

    sleeps = Sleeps()
    with pytest.raises(UpstreamUnavailable, match="timed out"):
        await make(handler, sleeps, retry_attempts=0).respond(req())
    assert len(calls) == 1 and sleeps.delays == []


async def test_429_uses_retry_after_capped() -> None:
    statuses = iter([(429, {"retry-after": "7"}), (429, {"retry-after": "9999"}), (200, {})])

    def handler(request: httpx.Request) -> httpx.Response:
        status, headers = next(statuses)
        if status == 200:
            return httpx.Response(200, json=OK_BODY)
        return httpx.Response(status, headers=headers, text="slow")

    sleeps = Sleeps()
    response = await make(handler, sleeps).respond(req())
    assert response.status == "completed"
    assert sleeps.delays == [7.0, 30.0]


async def test_429_without_retry_after_uses_backoff_and_exhausts() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="slow")

    sleeps = Sleeps()
    with pytest.raises(RateLimited):
        await make(handler, sleeps, retry_backoff=(1.0,)).respond(req())
    assert sleeps.delays == [1.0, 1.0]


async def test_4xx_is_not_retried() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(400, text="bad")

    sleeps = Sleeps()
    with pytest.raises(BadRequest):
        await make(handler, sleeps).respond(req())
    assert len(calls) == 1 and sleeps.delays == []


async def test_no_retry_after_partial_stream() -> None:
    calls: list[int] = []
    body = (
        b'data: {"type":"response.output_text.delta","delta":"par"}\n\n'
        # stream ends without a terminal event
    )

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    sleeps = Sleeps()
    seen: list[object] = []
    with pytest.raises(UpstreamUnavailable, match="closed before"):
        async for event in make(handler, sleeps).respond_stream(req()):
            seen.append(event)
    assert [type(e) for e in seen] == [TextDelta]
    assert len(calls) == 1 and sleeps.delays == []


async def test_stream_open_failure_is_retried() -> None:
    calls: list[int] = []
    done = (
        b'data: {"type":"response.completed","response":'
        b'{"id":"r","status":"completed","output":[]}}\n\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(200, content=done, headers={"content-type": "text/event-stream"})

    sleeps = Sleeps()
    events = [e async for e in make(handler, sleeps).respond_stream(req())]
    assert len(calls) == 2 and sleeps.delays == [2.0]
    assert events[-1].response.status == "completed"  # type: ignore[union-attr]


async def test_build_provider_passes_retry_config() -> None:
    cfg = ProviderConfig(
        base_url="https://up.example/v1",
        model="m",
        context_window=1000,
        api_key=Secret("k"),
        reasoning=ReasoningTiers(),
        retry_attempts=4,
        retry_backoff=(1.0, 3.0),
    )
    provider = build_provider(cfg)
    assert provider._retry_attempts == 4  # noqa: SLF001
    assert provider._retry_backoff == (1.0, 3.0)  # noqa: SLF001
    await provider.aclose()
