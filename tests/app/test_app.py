"""App wiring: scope drop, identity/session routing, dispatcher, reply (offline).

No real NapCat or upstream: the provider is scripted and ``napcat.send`` is replaced by a fake.
The dispatcher's clock is fake, so the 5 s quiet window is advanced explicitly.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from cgbot.app import RECORD_FAILED, App, render_segments
from cgbot.config import Config, load_config
from cgbot.dispatch import QUIET_SECONDS
from cgbot.model import Channel, InboundMessage, InputStatus, OutboundMessage, Segment, SendResult
from cgbot.provider import FunctionCall, ModelRequest, ModelResponse, TextOutput, Usage
from cgbot.scheduler.events import KIND_COMPACTION_IDLE, SystemEvent


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


class FakeTimebase:
    """``advance`` moves time forward and wakes every sleeper whose deadline has passed."""

    def __init__(self) -> None:
        self._now = 0.0
        self._sleepers: list[tuple[float, asyncio.Future[None]]] = []

    def now(self) -> float:
        return self._now

    async def sleep(self, seconds: float) -> None:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._sleepers.append((self._now + seconds, future))
        await future

    def advance(self, seconds: float) -> None:
        self._now += seconds
        for deadline, future in self._sleepers:
            if deadline <= self._now and not future.done():
                future.set_result(None)
        self._sleepers = [(d, f) for d, f in self._sleepers if not f.done()]


def make_config(tmp_path: Path) -> Config:
    return make_config_extra(tmp_path, "")


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


def typing(user_id: str) -> InputStatus:
    return InputStatus(user_id=user_id, event_type=1, status_text="对方正在输入", time=123.0)


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


async def flush() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


async def wait_until(condition: Callable[[], bool]) -> None:
    for _ in range(200):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition not reached")


async def settle(app: App, clock: FakeTimebase) -> None:
    """Advance past quiet windows until the dispatcher has no lane left (turns finished)."""
    for _ in range(100):
        tasks = [
            lane.task
            for lane in app._dispatcher._lanes.values()  # noqa: SLF001 - test drains lanes
            if lane.task is not None
        ]
        if not tasks:
            return
        clock.advance(QUIET_SECONDS)
        await asyncio.wait(tasks, timeout=0.5)
    raise AssertionError("app did not settle")


def reply_script(text: str) -> list[ModelResponse]:
    return [calls_resp(("c1", "reply", {"text": text})), text_resp("done")]


async def test_out_of_scope_private_is_dropped_before_identity_and_llm(tmp_path: Path) -> None:
    clock = FakeTimebase()
    provider = ScriptedProvider()
    app = App(make_config(tmp_path), provider, timebase=clock)
    recorder = Recorder()
    install(app, recorder)

    await app.handle_message(private_msg("m1", OUT_OF_SCOPE, "hi"))
    await settle(app, clock)

    assert provider.requests == []
    assert recorder.sent == []
    assert await app._sessions.list_sessions() == []  # noqa: SLF001
    await app.stop()


async def test_group_message_is_dropped(tmp_path: Path) -> None:
    clock = FakeTimebase()
    provider = ScriptedProvider()
    app = App(make_config(tmp_path), provider, timebase=clock)
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
    await settle(app, clock)

    assert provider.requests == []
    assert recorder.sent == []
    await app.stop()


async def test_in_scope_private_reply_is_sent_via_reply_tool(tmp_path: Path) -> None:
    clock = FakeTimebase()
    provider = ScriptedProvider(*reply_script("你好"))
    app = App(make_config(tmp_path), provider, timebase=clock)
    recorder = Recorder()
    install(app, recorder)

    await app.handle_message(private_msg("m1", IN_SCOPE, "嗨"))
    await settle(app, clock)

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
    clock = FakeTimebase()
    provider = ScriptedProvider(*reply_script("a"), *reply_script("b"))
    app = App(make_config(tmp_path), provider, timebase=clock)
    recorder = Recorder()
    install(app, recorder)

    await app.handle_message(private_msg("m1", IN_SCOPE, "一"))
    await settle(app, clock)
    await app.handle_message(private_msg("m2", IN_SCOPE, "二"))
    await settle(app, clock)

    identity = await app._identity.resolve(_qq(IN_SCOPE))  # noqa: SLF001
    assert identity is not None
    assert len(identity.channels) == 1
    sessions = await app._sessions.list_sessions()  # noqa: SLF001
    assert sessions == [f"private:{identity.user_id}"]
    assert [m.target_id for m in recorder.sent] == [IN_SCOPE, IN_SCOPE]
    await app.stop()


async def test_message_during_running_turn_interrupts_and_restarts_with_new_only(
    tmp_path: Path,
) -> None:
    clock = FakeTimebase()
    gate = asyncio.Event()  # never set: the first model call stays in flight until cancelled
    provider = ScriptedProvider(*reply_script("收到"), gate=gate)
    app = App(make_config(tmp_path), provider, timebase=clock)
    recorder = Recorder()
    install(app, recorder)

    await app.handle_message(private_msg("m1", IN_SCOPE, "第一条"))
    clock.advance(QUIET_SECONDS)
    await wait_until(lambda: len(provider.requests) == 1)  # first model call is in flight

    await app.handle_message(private_msg("m2", IN_SCOPE, "第二条"))
    await settle(app, clock)

    assert len(provider.requests) == 3
    restarted = json.dumps(provider.requests[1].input, ensure_ascii=False)
    assert restarted.count("第一条") == 1
    assert restarted.count("第二条") == 1
    assert [m.segments for m in recorder.sent] == [(Segment("text", {"text": "收到"}),)]
    await app.stop()


async def test_typing_defers_turn_until_user_is_quiet(tmp_path: Path) -> None:
    clock = FakeTimebase()
    provider = ScriptedProvider(*reply_script("好"))
    app = App(make_config(tmp_path), provider, timebase=clock)
    install(app, Recorder())

    await app.handle_message(private_msg("m1", IN_SCOPE, "嗨"))  # quiet until t=5
    clock.advance(4)
    await app.handle_input_status(typing(IN_SCOPE))  # quiet until t=9
    clock.advance(1)  # t=5: the old deadline passes, the lane re-arms
    await flush()
    clock.advance(3)  # t=8
    await flush()
    assert provider.requests == []

    clock.advance(1)  # t=9
    await settle(app, clock)
    assert len(provider.requests) == 2
    await app.stop()


async def test_input_status_from_out_of_scope_user_is_ignored(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    clock = FakeTimebase()
    app = App(config, ScriptedProvider(), timebase=clock)

    await app.handle_input_status(typing(OUT_OF_SCOPE))

    assert app._dispatcher._lanes == {}  # noqa: SLF001
    conn = sqlite3.connect(config.storage.profile)
    try:
        count = conn.execute("SELECT COUNT(*) FROM identity_channels").fetchone()
    finally:
        conn.close()
    assert count is not None
    assert count[0] == 0
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
        names = {p.name for p in data_dir.glob("*.sqlite")}
        assert {"profile.sqlite", "scheduler.sqlite", "sessions.sqlite"} <= names
        assert "cgbot.sqlite" not in names
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


def _qq(account: str) -> Channel:
    return Channel("qq", account)


@pytest.mark.parametrize("segments", [(), (Segment("reply", {"id": "1"}),)])
def test_render_segments_empty_when_no_content(segments: tuple[Segment, ...]) -> None:
    assert render_segments(segments) == ""


class FakeGate:
    """Stands in for GroupGate: fixed decision; records context takes and bot speech."""

    def __init__(self, action: str, context: tuple[Any, ...] = ()) -> None:
        self.action = action
        self.context = context
        self.taken: list[str] = []
        self.spoke: list[tuple[str, float, str | None]] = []

    async def decide(self, message: InboundMessage) -> SimpleNamespace:
        return SimpleNamespace(action=self.action)

    def take_context(self, group_id: str) -> tuple[Any, ...]:
        self.taken.append(group_id)
        return self.context

    def note_bot_spoke(self, group_id: str, at: float, message_id: str | None) -> None:
        self.spoke.append((group_id, at, message_id))


class FakeAsr:
    def __init__(self, text: str = "", error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.received: list[bytes] = []

    async def transcribe(self, wav: bytes, *, language: str | None = None) -> SimpleNamespace:
        self.received.append(wav)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(text=self.text)


class FakeMedia:
    async def to_asr_wav(self, audio: bytes) -> bytes:
        return b"RIFF" + audio


def group_msg(message_id: str = "g1") -> InboundMessage:
    return InboundMessage(
        message_id=message_id,
        scope="group",
        user_id=IN_SCOPE,
        group_id="2001",
        segments=(Segment("text", {"text": "大家好"}),),
        time=1.0,
        reply_to=None,
    )


def install_voice(app: App, asr: FakeAsr) -> None:
    voice = SimpleNamespace(asr=asr, tts=None, media=FakeMedia(), services=())
    app._voice = cast(Any, voice)  # noqa: SLF001 - test injects fake voice clients


def install_record_file(app: App, path: Path) -> None:
    async def fake_call(action: str, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        assert action == "get_record"
        return {"data": {"file": str(path)}}

    app.napcat.call = fake_call  # type: ignore[method-assign]


async def test_private_turn_registers_memory_tools(tmp_path: Path) -> None:
    clock = FakeTimebase()
    provider = ScriptedProvider(*reply_script("好的"))
    app = App(make_config(tmp_path), provider, timebase=clock)
    install(app, Recorder())

    await app.handle_message(private_msg("m1", IN_SCOPE, "记住我喜欢猫"))
    await settle(app, clock)

    tools = json.dumps(provider.requests[0].tools)
    assert "memory_add" in tools
    assert "memory_search" in tools
    assert "memory_forget" in tools
    assert "send_voice" not in tools  # no [voice] table: voice disabled
    await app.stop()


async def test_send_voice_tool_registered_only_when_voice_enabled(tmp_path: Path) -> None:
    clock = FakeTimebase()
    provider = ScriptedProvider(*reply_script("好的"))
    app = App(make_config_extra(tmp_path, "\n[voice]\n"), provider, timebase=clock)
    install(app, Recorder())

    await app.handle_message(private_msg("m1", IN_SCOPE, "你好"))
    await settle(app, clock)

    assert "send_voice" in json.dumps(provider.requests[0].tools)
    await app.stop()


async def test_group_wake_hands_buffered_context_to_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = App(make_config(tmp_path), ScriptedProvider(), timebase=FakeTimebase())
    gate = FakeGate("wake", context=("ctx-1", "ctx-2"))
    monkeypatch.setattr(app, "_group_gate", lambda: gate)
    calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fake_handle(
        message: InboundMessage, trigger: str, buffered: tuple[Any, ...] = ()
    ) -> None:
        calls.append((trigger, tuple(buffered)))

    monkeypatch.setattr(app._groups, "handle", fake_handle)  # noqa: SLF001
    await app.handle_message(group_msg())
    await asyncio.gather(*list(app._tasks))  # noqa: SLF001

    assert gate.taken == ["2001"]
    assert calls == [("wake", ("ctx-1", "ctx-2"))]
    await app.stop()


async def test_group_mute_trigger_calls_manager_mute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = App(make_config(tmp_path), ScriptedProvider(), timebase=FakeTimebase())
    monkeypatch.setattr(app, "_group_gate", lambda: FakeGate("mute_triggered"))
    muted: list[str] = []

    async def fake_mute(group_id: str) -> None:
        muted.append(group_id)

    monkeypatch.setattr(app._groups, "mute", fake_mute)  # noqa: SLF001
    await app.handle_message(group_msg())
    await asyncio.gather(*list(app._tasks))  # noqa: SLF001

    assert muted == ["2001"]
    await app.stop()


async def test_group_send_notifies_gate_of_bot_speech(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = App(make_config(tmp_path), ScriptedProvider(), timebase=FakeTimebase())
    gate = FakeGate("none")
    monkeypatch.setattr(app, "_gate", gate)
    install(app, Recorder())

    result = await app._send_group(  # noqa: SLF001
        OutboundMessage(
            scope="group",
            target_id="2001",
            segments=(Segment("text", {"text": "嗨"}),),
        )
    )

    assert result.ok
    assert [(gid, mid) for gid, _at, mid in gate.spoke] == [("2001", "out-1")]
    await app.stop()


async def test_idle_compaction_event_is_routed_to_compactor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = App(make_config(tmp_path), ScriptedProvider(), timebase=FakeTimebase())
    seen: list[str] = []

    async def fake_idle(event: SystemEvent) -> SimpleNamespace:
        seen.append(event.event_id)
        return SimpleNamespace(status="skipped")

    monkeypatch.setattr(app._compactor, "handle_idle_event", fake_idle)  # noqa: SLF001
    event = SystemEvent(
        event_id="idle-1",
        kind=KIND_COMPACTION_IDLE,
        user_id=IN_SCOPE,
        payload={},
        due_at=0.0,
    )
    await app._on_system_event(event)  # noqa: SLF001
    await asyncio.gather(*app._tasks)  # noqa: SLF001
    assert seen == ["idle-1"]
    await app.stop()


async def test_private_record_is_transcribed_into_turn_text(tmp_path: Path) -> None:
    app = App(make_config(tmp_path), ScriptedProvider(), timebase=FakeTimebase())
    audio = tmp_path / "voice.silk"
    audio.write_bytes(b"silk")
    asr = FakeAsr(text="你好呀")
    install_voice(app, asr)
    install_record_file(app, audio)

    text = await app._render_private((Segment("record", {"file": "f.silk"}),))  # noqa: SLF001

    assert text == "[语音转写: 你好呀]"
    assert asr.received == [b"RIFFsilk"]
    await app.stop()


async def test_private_record_asr_failure_gives_placeholder(tmp_path: Path) -> None:
    app = App(make_config(tmp_path), ScriptedProvider(), timebase=FakeTimebase())
    audio = tmp_path / "voice.silk"
    audio.write_bytes(b"silk")
    install_voice(app, FakeAsr(error=RuntimeError("asr down")))
    install_record_file(app, audio)

    text = await app._render_private((Segment("record", {"file": "f.silk"}),))  # noqa: SLF001

    assert text == RECORD_FAILED
    await app.stop()


async def test_private_record_without_voice_is_plain_placeholder(tmp_path: Path) -> None:
    app = App(make_config(tmp_path), ScriptedProvider(), timebase=FakeTimebase())

    text = await app._render_private((Segment("record", {"file": "f.silk"}),))  # noqa: SLF001

    assert text == "[语音]"
    await app.stop()


def make_config_extra(tmp_path: Path, extra: str) -> Config:
    secrets = tmp_path / "secrets.toml"
    secrets.write_text('[secrets]\nprovider_api_key = "sk-test-APP"\n', encoding="utf-8")
    secrets.chmod(0o600)
    config_file = tmp_path / "config.toml"
    data_dir = tmp_path / "data"
    config_file.write_text(
        BASE_TOML + f'\n[storage]\ndata_dir = "{data_dir}"\n' + extra, encoding="utf-8"
    )
    return load_config(config_file, secrets, env={})
