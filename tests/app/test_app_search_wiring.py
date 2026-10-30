"""App wiring for search tools: web_search offered to private turns, closed on stop (offline)."""

from __future__ import annotations

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

from cgbot.app import App


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
