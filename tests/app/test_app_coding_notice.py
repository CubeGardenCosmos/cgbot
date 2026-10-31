"""App wiring for Coding Agent notices: the notice is recorded and a private turn summarizes it."""

from __future__ import annotations

import json
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
from cgbot.coding.contracts import CodingNotice
from cgbot.coding.notifier import QqNotifier
from cgbot.model import Channel, OutboundMessage, SendResult
from cgbot.provider import user_message


def _app(tmp_path: Path, provider: ScriptedProvider, clock: FakeTimebase) -> App:
    app = App(make_config(tmp_path), provider, timebase=clock)
    install(app, Recorder())
    return app


async def _internal_user_id(app: App) -> str:
    identity = await app._identity.resolve(Channel("qq", IN_SCOPE))  # noqa: SLF001
    return identity.user_id


def _inputs(request_input: list[dict]) -> str:
    return json.dumps(request_input, ensure_ascii=False)


async def test_coding_notice_is_summarized_by_one_private_turn_not_sent_as_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    quiet_watchers(monkeypatch)
    clock = FakeTimebase()
    provider = ScriptedProvider(text_resp("好"), text_resp("论文做完啦"))
    app = _app(tmp_path, provider, clock)
    sent: list[OutboundMessage] = []

    async def send(message: OutboundMessage) -> SendResult:
        sent.append(message)
        return SendResult(ok=True, message_id=None)

    await app.handle_message(private_msg("m1", IN_SCOPE, "你好"))
    await settle(app, clock)
    internal = await _internal_user_id(app)
    notifier = QqNotifier(send, app._sessions, app._start_coding_notice_turn)  # noqa: SLF001

    await notifier.notify(
        CodingNotice(
            kind="turn_completed",
            user_id=internal,
            thread_id="t1",
            title="论文",
            text="完成了",
            status="completed",
        )
    )
    await settle(app, clock)

    assert sent == []  # the text reaches the user only through the agent's own reply
    assert len(provider.requests) == 2
    assert "任务完成：论文" in _inputs(provider.requests[1].input)
    await app.stop()


async def test_notice_trigger_rejects_session_that_is_not_the_users_private_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    quiet_watchers(monkeypatch)
    clock = FakeTimebase()
    provider = ScriptedProvider(text_resp("好"))
    app = _app(tmp_path, provider, clock)
    await app.handle_message(private_msg("m1", IN_SCOPE, "你好"))
    await settle(app, clock)
    internal = await _internal_user_id(app)

    with pytest.raises(ValueError):
        await app._start_coding_notice_turn(internal, "private:someone-else")  # noqa: SLF001
    await app.stop()


async def test_notice_trigger_rejects_unknown_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    quiet_watchers(monkeypatch)
    clock = FakeTimebase()
    provider = ScriptedProvider(text_resp("好"))
    app = _app(tmp_path, provider, clock)

    with pytest.raises(LookupError):
        await app._start_coding_notice_turn("no-such-user", "private:no-such-user")  # noqa: SLF001
    assert provider.requests == []
    await app.stop()


async def test_notice_record_is_stored_as_system_event_with_model_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    quiet_watchers(monkeypatch)
    clock = FakeTimebase()
    provider = ScriptedProvider(text_resp("好"), text_resp("收到"))
    app = _app(tmp_path, provider, clock)
    await app.handle_message(private_msg("m1", IN_SCOPE, "你好"))
    await settle(app, clock)
    internal = await _internal_user_id(app)
    session_id = await app._identity.private_session_id(internal)  # noqa: SLF001
    await app._sessions.append(  # noqa: SLF001
        session_id,
        "system_event",
        {"event": "coding_notice", "item": user_message("[系统通知] 任务完成")},
    )

    await app._start_coding_notice_turn(internal, session_id)  # noqa: SLF001
    await settle(app, clock)

    assert "[系统通知] 任务完成" in _inputs(provider.requests[1].input)
    items = await app._sessions.load(session_id)  # noqa: SLF001
    user_rows = [item.kind for item in items if item.kind == "user"]
    assert user_rows == ["user"]  # the notice itself is a system_event row
    await app.stop()
