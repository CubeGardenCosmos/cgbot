"""M1 app wiring: scope drop, identity/session routing, reply, injection (offline).

No real NapCat or upstream: the provider is scripted and ``napcat.send`` is replaced by a fake.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from cgbot.app import App, render_segments
from cgbot.config import Config, load_config
from cgbot.model import InboundMessage, InputStatus, OutboundMessage, Segment, SendResult
from cgbot.provider import FunctionCall, ModelRequest, ModelResponse, TextOutput, Usage


def _tables(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    finally:
        conn.close()
    return {str(r[0]) for r in rows}


BASE_TOML = """
[napcat]
port = 6199

[provider]
base_url = "https://up.example/v1"
model = "model-x"
context_window = 128000
api_key_ref = "provider_api_key"

[scope]
friends = [1001, "1002"]
groups = [2001]
"""

IN_SCOPE = "1001"
OUT_OF_SCOPE = "9999"


def make_config(tmp_path: Path) -> Config:
    secrets = tmp_path / "secrets.toml"
    secrets.write_text('[secrets]\nprovider_api_key = "sk-test-APP"\n', encoding="utf-8")
    secrets.chmod(0o600)
    config_file = tmp_path / "config.toml"
    data_dir = tmp_path / "data"
    config_file.write_text(BASE_TOML + f'\n[storage]\ndata_dir = "{data_dir}"\n', encoding="utf-8")
    return load_config(config_file, secrets, env={})


def private_msg(
    message_id: str, user_id: str, text: str, reply_to: str | None = None
) -> InboundMessage:
    return InboundMessage(
        message_id=message_id,
        scope="private",
        user_id=user_id,
        group_id=None,
        segments=(Segment("text", {"text": text}),),
        time=1_700_000_000.0,
        reply_to=reply_to,
    )


def calls_resp(*calls: tuple[str, str, dict[str, Any]]) -> ModelResponse:
    items: list[Any] = []
    for call_id, name, args in calls:
        arguments = json.dumps(args)
        raw = {"type": "function_call", "call_id": call_id, "name": name, "arguments": arguments}
        items.append(
            FunctionCall(
                call_id=call_id,
                name=name,
                arguments=arguments,
                parsed_arguments=args,
                parse_error=None,
                raw=raw,
            )
        )
    return ModelResponse(items=tuple(items), usage=Usage(), response_id="r", status="completed")


def text_resp(text: str) -> ModelResponse:
    raw = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text}],
    }
    return ModelResponse(
        items=(TextOutput(text=text, raw=raw),),
        usage=Usage(),
        response_id="r",
        status="completed",
    )


class ScriptedProvider:
    """Scripted responses; optionally the first call waits on ``gate`` before answering."""

    def __init__(self, *script: ModelResponse, gate: asyncio.Event | None = None) -> None:
        self._script = list(script)
        self._gate = gate
        self.requests: list[ModelRequest] = []

    @property
    def name(self) -> str:
        return "scripted"

    @property
    def context_window(self) -> int:
        return 100_000

    async def respond(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if self._gate is not None and len(self.requests) == 1:
            await self._gate.wait()
        if not self._script:
            raise AssertionError("no scripted response left")
        return self._script.pop(0)

    def respond_stream(self, request: ModelRequest) -> AsyncIterator[Any]:
        raise NotImplementedError


class Recorder:
    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []

    async def __call__(self, message: OutboundMessage) -> SendResult:
        self.sent.append(message)
        return SendResult(ok=True, message_id=f"out-{len(self.sent)}", error=None)


def install(app: App, recorder: Recorder) -> None:
    app.napcat.send = recorder  # type: ignore[method-assign]


async def settle(app: App) -> None:
    while app._turns:  # noqa: SLF001 - test drains background turns
        await asyncio.gather(*list(app._turns.values()))  # noqa: SLF001


def reply_script(text: str) -> list[ModelResponse]:
    return [calls_resp(("c1", "reply", {"text": text})), text_resp("done")]


async def test_out_of_scope_private_is_dropped_before_identity_and_llm(tmp_path: Path) -> None:
    provider = ScriptedProvider()
    app = App(make_config(tmp_path), provider)
    recorder = Recorder()
    install(app, recorder)

    await app.handle_message(private_msg("m1", OUT_OF_SCOPE, "hi"))
    await settle(app)

    assert provider.requests == []
    assert recorder.sent == []
    assert await app._sessions.list_sessions() == []  # noqa: SLF001
    await app.stop()


async def test_group_message_is_dropped(tmp_path: Path) -> None:
    provider = ScriptedProvider()
    app = App(make_config(tmp_path), provider)
    recorder = Recorder()
    install(app, recorder)

    group_msg = InboundMessage(
        message_id="g1",
        scope="group",
        user_id=IN_SCOPE,
        group_id="2001",
        segments=(Segment("text", {"text": "hello group"}),),
        time=1.0,
        reply_to=None,
    )
    await app.handle_message(group_msg)
    await settle(app)

    assert provider.requests == []
    assert recorder.sent == []
    await app.stop()


async def test_in_scope_private_reply_is_sent_via_reply_tool(tmp_path: Path) -> None:
    provider = ScriptedProvider(*reply_script("你好"))
    app = App(make_config(tmp_path), provider)
    recorder = Recorder()
    install(app, recorder)

    await app.handle_message(private_msg("m1", IN_SCOPE, "嗨"))
    await settle(app)

    assert len(recorder.sent) == 1
    sent = recorder.sent[0]
    assert sent.scope == "private"
    assert sent.target_id == IN_SCOPE
    assert sent.reply_to is None
    assert sent.segments == (Segment("text", {"text": "你好"}),)
    assert len(provider.requests) == 2
    assert provider.requests[0].instructions
    await app.stop()


async def test_identity_and_session_are_created_once_per_user(tmp_path: Path) -> None:
    provider = ScriptedProvider(*reply_script("a"), *reply_script("b"))
    app = App(make_config(tmp_path), provider)
    recorder = Recorder()
    install(app, recorder)

    await app.handle_message(private_msg("m1", IN_SCOPE, "一"))
    await settle(app)
    await app.handle_message(private_msg("m2", IN_SCOPE, "二"))
    await settle(app)

    identity = await app._identity.resolve(_qq(IN_SCOPE))  # noqa: SLF001
    assert identity is not None
    assert len(identity.channels) == 1
    sessions = await app._sessions.list_sessions()  # noqa: SLF001
    assert sessions == [f"private:{identity.user_id}"]
    assert [m.target_id for m in recorder.sent] == [IN_SCOPE, IN_SCOPE]
    await app.stop()


async def test_message_while_turn_running_is_injected(tmp_path: Path) -> None:
    gate = asyncio.Event()
    provider = ScriptedProvider(
        calls_resp(("c1", "reply", {"text": "第一条回复"})),
        text_resp("done"),
        gate=gate,
    )
    app = App(make_config(tmp_path), provider)
    recorder = Recorder()
    install(app, recorder)

    await app.handle_message(private_msg("m1", IN_SCOPE, "第一条"))
    await asyncio.sleep(0)
    sid = f"private:{(await app._identity.resolve(_qq(IN_SCOPE))).user_id}"  # noqa: SLF001
    assert app._runner.is_running(sid)  # noqa: SLF001

    await app.handle_message(private_msg("m2", IN_SCOPE, "第二条"))
    gate.set()
    await settle(app)

    assert len(provider.requests) == 2
    assert "第二条" in json.dumps(provider.requests[1].input, ensure_ascii=False)
    assert [m.segments for m in recorder.sent] == [(Segment("text", {"text": "第一条回复"}),)]
    await app.stop()


async def test_input_status_records_last_time(tmp_path: Path) -> None:
    app = App(make_config(tmp_path), ScriptedProvider())
    assert app.last_input_time(IN_SCOPE) is None

    await app.handle_input_status(
        InputStatus(user_id=IN_SCOPE, event_type=1, status_text="对方正在输入", time=123.0)
    )
    assert app.last_input_time(IN_SCOPE) == 123.0
    await app.stop()


async def test_stop_without_start_is_clean(tmp_path: Path) -> None:
    app = App(make_config(tmp_path), ScriptedProvider())
    await app.stop()


async def test_app_wires_one_database_file_per_domain(tmp_path: Path) -> None:
    """Round 20: profile / sessions / scheduler each get their own SQLite file."""
    config = make_config(tmp_path)
    app = App(config, ScriptedProvider())
    try:
        data_dir = config.storage.data_dir
        assert sorted(p.name for p in data_dir.glob("*.sqlite")) == [
            "profile.sqlite",
            "scheduler.sqlite",
            "sessions.sqlite",
        ]
        assert not (data_dir / "cgbot.sqlite").exists()  # the old shared file is gone
        assert _tables(config.storage.profile) == {
            "identity_channels",
            "identities",
            "schema_version",
        }
        assert _tables(config.storage.sessions) == {"schema_version", "session_items"}
        assert _tables(config.storage.scheduler) == {"scheduled_events", "schema_version"}
    finally:
        await app.stop()


def test_render_segments_text_and_placeholders() -> None:
    segments = (
        Segment("text", {"text": "看这个"}),
        Segment("image", {"file": "x.png"}),
        Segment("reply", {"id": "9"}),
        Segment("at", {"qq": "1001"}),
        Segment("record", {"file": "a.amr"}),
    )
    assert render_segments(segments) == "看这个[图片]@1001[语音]"
    assert render_segments(()) == ""


def _qq(account: str) -> Any:
    from cgbot.model import Channel

    return Channel("qq", account)


@pytest.mark.parametrize("segments", [(), (Segment("reply", {"id": "1"}),)])
def test_render_segments_empty_when_no_content(segments: tuple[Segment, ...]) -> None:
    assert render_segments(segments) == ""
