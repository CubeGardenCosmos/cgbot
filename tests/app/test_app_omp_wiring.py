"""App wiring for the omp tool family: private-only registration, ``wake_private``, secrecy, stop.

Reuses the App fakes from ``test_app`` and the omp HTTP fake; no network is touched.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from omp_fakes import FakeOmpweb, quiet_watchers
from test_app import (
    IN_SCOPE,
    OUT_OF_SCOPE,
    FakeTimebase,
    Recorder,
    ScriptedProvider,
    calls_resp,
    install,
    make_config,
    private_msg,
    settle,
    text_resp,
)

from cgbot.app import App
from cgbot.model import Channel

PW = "pw-secret-123"


def _inputs(request_input: list[dict]) -> str:
    return json.dumps(request_input, ensure_ascii=False)


def _app(tmp_path: Path, provider: ScriptedProvider, clock: FakeTimebase) -> App:
    app = App(
        make_config(tmp_path),
        provider,
        timebase=clock,
        omp_http_client_factory=FakeOmpweb().factory(),
    )
    install(app, Recorder())
    return app


async def _internal_user_id(app: App) -> str:
    """wake_private takes the internal user_id (the identity's), not the QQ account."""
    identity = await app._identity.resolve(Channel("qq", IN_SCOPE))  # noqa: SLF001
    return identity.user_id


async def test_omp_tools_are_offered_to_private_turns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    quiet_watchers(monkeypatch)
    clock = FakeTimebase()
    provider = ScriptedProvider(text_resp("好"))
    app = _app(tmp_path, provider, clock)

    await app.handle_message(private_msg("m1", IN_SCOPE, "你好"))
    await settle(app, clock)

    offered = {tool["name"] for tool in provider.requests[0].tools}
    assert {"omp_add", "omp_task_create", "omp_task_report"} <= offered
    await app.stop()


async def test_wake_private_starts_a_turn_with_the_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    quiet_watchers(monkeypatch)
    clock = FakeTimebase()
    provider = ScriptedProvider(text_resp("好"), text_resp("已收到通知"))
    app = _app(tmp_path, provider, clock)

    await app.handle_message(private_msg("m1", IN_SCOPE, "你好"))
    await settle(app, clock)
    internal = await _internal_user_id(app)
    await app.wake_private(internal, "任务 A 已完成")
    await settle(app, clock)

    assert len(provider.requests) == 2
    assert "[系统通知] 任务 A 已完成" in _inputs(provider.requests[1].input)
    await app.stop()


async def test_wake_private_for_unknown_user_is_dropped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    quiet_watchers(monkeypatch)
    clock = FakeTimebase()
    provider = ScriptedProvider()
    app = _app(tmp_path, provider, clock)

    with caplog.at_level(logging.WARNING, logger="cgbot.app"):
        await app.wake_private(OUT_OF_SCOPE, "无人接收")
    await settle(app, clock)

    assert provider.requests == []
    assert "private wake dropped" in caplog.text
    await app.stop()


async def test_omp_add_never_logs_or_returns_the_password(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    quiet_watchers(monkeypatch)
    clock = FakeTimebase()
    args = {"host": "127.0.0.1", "port": 8080, "password": PW, "alias": "box"}
    provider = ScriptedProvider(calls_resp(("c1", "omp_add", args)), text_resp("已添加"))
    app = _app(tmp_path, provider, clock)

    with caplog.at_level(logging.DEBUG):
        await app.handle_message(private_msg("m1", IN_SCOPE, "添加 box"))
        await settle(app, clock)

    assert len(provider.requests) == 2
    outputs = [i for i in provider.requests[1].input if i.get("type") == "function_call_output"]
    assert outputs, "omp_add result should be fed back to the model"
    assert PW not in _inputs(outputs)
    assert PW not in caplog.text
    await app.stop()


async def test_stop_closes_the_omp_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    quiet_watchers(monkeypatch)
    clock = FakeTimebase()
    app = _app(tmp_path, ScriptedProvider(), clock)

    await app.stop()

    assert app._omp.closed  # noqa: SLF001 - lifecycle is the thing under test
