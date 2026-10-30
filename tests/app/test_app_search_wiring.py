"""App wiring for search tools: web_search offered to private turns, closed on stop (offline)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from omp_fakes import quiet_watchers
from test_app import (
    IN_SCOPE,
    FakeTimebase,
    Recorder,
    ScriptedProvider,
    install,
    make_config,
    private_msg,
    settle,
    text_resp,
)

import cgbot.app as app_module
from cgbot.agent.tools import ToolSpec
from cgbot.app import App
from cgbot.config.settings import SearchGeminiConfig, Secret
from cgbot.search import SearchService, search_tools
from cgbot.search.gemini import GeminiSearchClient


def _app(tmp_path: Path, provider: ScriptedProvider, clock: FakeTimebase) -> App:
    app = App(make_config(tmp_path), provider, timebase=clock)
    install(app, Recorder())
    return app


async def test_web_search_is_offered_to_private_turns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    quiet_watchers(monkeypatch)
    clock = FakeTimebase()
    provider = ScriptedProvider(text_resp("好"))
    app = _app(tmp_path, provider, clock)

    await app.handle_message(private_msg("m1", IN_SCOPE, "你好"))
    await settle(app, clock)

    offered = {tool["name"] for tool in provider.requests[0].tools}
    assert "web_search" in offered
    assert "gemini_search" not in offered  # [search.gemini] is disabled by default
    await app.stop()


async def test_stop_closes_the_search_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    quiet_watchers(monkeypatch)
    clock = FakeTimebase()
    app = _app(tmp_path, ScriptedProvider(), clock)
    closed: list[bool] = []

    async def fake_close() -> None:
        closed.append(True)

    monkeypatch.setattr(app._search, "close", fake_close)  # noqa: SLF001 - lifecycle under test
    await app.stop()

    assert closed == [True]


async def test_enabled_gemini_client_is_shared_with_search_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    quiet_watchers(monkeypatch)
    config = make_config(tmp_path)
    config = replace(
        config,
        search=replace(
            config.search,
            gemini=SearchGeminiConfig(
                enabled=True,
                endpoint="https://relay.example/search",
                token=Secret("test-gemini-token"),
            ),
        ),
    )
    received: list[tuple[SearchService, GeminiSearchClient | None]] = []

    def tools(
        service: SearchService, *, gemini_client: GeminiSearchClient | None = None
    ) -> list[ToolSpec]:
        received.append((service, gemini_client))
        return search_tools(service, gemini_client=gemini_client)

    monkeypatch.setattr(app_module, "search_tools", tools)
    clock = FakeTimebase()
    provider = ScriptedProvider(text_resp("好"))
    app = App(config, provider, timebase=clock)
    install(app, Recorder())

    await app.handle_message(private_msg("m1", IN_SCOPE, "你好"))
    await settle(app, clock)

    assert received == [(app._search, app._gemini)]  # noqa: SLF001 - wiring under test
    assert isinstance(received[0][1], GeminiSearchClient)
    offered = {tool["name"] for tool in provider.requests[0].tools}
    assert {"web_search", "gemini_search"} <= offered
    await app.stop()
