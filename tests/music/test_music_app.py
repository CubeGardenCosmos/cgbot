"""私聊乐谱文件管线与技能注入(App 层);群聊路径从不暂存、从不注入。离线。"""

from __future__ import annotations

import base64
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from music_fakes import music_config

from cgbot.app import MUSIC_PROMPT, App
from cgbot.coding.contracts import SubmitResult, workspace_dir
from cgbot.config import Config
from cgbot.dispatch import PrivateMessage
from cgbot.model import Channel, InboundMessage, Segment
from cgbot.music.skill import load_skill_text
from cgbot.provider import ModelProvider

MIDI = b"MThd\x00\x00\x00\x06\x00\x00\x00\x01\x00\x60"
ENABLED = "[music]\nenabled = true\n"
SKILL = load_skill_text()


def _file(name: str = "song.mid", **extra: Any) -> Segment:
    return Segment("file", {"file": name, "file_id": "fid-1", **extra})


def _private(*segments: Segment) -> InboundMessage:
    return InboundMessage(
        message_id="m1",
        scope="private",
        user_id="1001",
        group_id=None,
        segments=segments,
        time=0.0,
    )


def _group(*segments: Segment) -> InboundMessage:
    return InboundMessage(
        message_id="g1",
        scope="group",
        user_id="1001",
        group_id="2001",
        segments=segments,
        time=0.0,
    )


class Harness:
    def __init__(self, app: App, config: Config, queued: list[PrivateMessage]) -> None:
        self.app = app
        self.config = config
        self.queued = queued
        self.napcat_calls: list[tuple[str, dict[str, object]]] = []
        self.submit = AsyncMock(
            return_value=SubmitResult(thread_id="t-music", state="started", queue_position=0)
        )

    @property
    def text(self) -> str:
        item = self.queued[-1].item
        assert item is not None
        content = cast(list[dict[str, str]], item["content"])
        return "".join(part["text"] for part in content if part["type"] == "input_text")

    async def inbox(self) -> list[Path]:
        assert self.config.coding is not None
        identity = await self.app._identity.resolve(Channel("qq", "1001"))
        inbox = workspace_dir(self.config.coding.docker.host_data_dir, identity.user_id) / "inbox"
        return sorted(inbox.iterdir()) if inbox.exists() else []


def _install(app: App, harness: Harness, response: Mapping[str, object] | Exception) -> None:
    async def call(
        action: str, params: dict[str, object] | None = None, timeout_s: float = 10.0
    ) -> dict[str, object]:
        harness.napcat_calls.append((action, dict(params or {})))
        if isinstance(response, Exception):
            raise response
        return dict(response)

    assert app._coding is not None
    app.napcat.call = call  # type: ignore[method-assign]
    app._coding.service.submit = harness.submit  # type: ignore[method-assign]


@pytest.fixture
def harnesses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    created: list[App] = []

    def make(
        music_toml: str = ENABLED,
        *,
        response: dict[str, object] | Exception | None = None,
        coding: bool = True,
    ) -> Harness:
        config = music_config(tmp_path, music_toml, coding=coding)
        app = App(config, cast(ModelProvider, object()))
        created.append(app)
        queued: list[PrivateMessage] = []
        monkeypatch.setattr(app._dispatcher, "on_message", queued.append)
        harness = Harness(app, config, queued)
        reply = (
            response
            if response is not None
            else {"data": {"base64": base64.b64encode(MIDI).decode()}}
        )
        if coding:
            _install(app, harness, reply)
        return harness

    yield make


async def _close(harness: Harness) -> None:
    await harness.app.stop()


async def test_private_midi_is_staged_notified_and_auto_rendered(harnesses: Any) -> None:
    h: Harness = harnesses()
    try:
        await h.app.handle_message(_private(Segment("text", {"text": "听听这首"}), _file()))
        files = await h.inbox()
        assert [f.read_bytes() for f in files] == [MIDI]
        assert files[0].name.startswith("score-")
        assert files[0].suffix == ".mid"
        sandbox_path = f"/workspace/inbox/{files[0].name}"
        assert h.napcat_calls == [("get_file", {"file_id": "fid-1"})]

        h.submit.assert_awaited_once()
        assert h.submit.await_args is not None
        user_id, prompt = h.submit.await_args.args
        assert user_id != "1001"  # 内部身份,不是 QQ 号
        assert sandbox_path in prompt
        assert "cgmusic render" in prompt
        assert "report_progress" in prompt
        assert h.submit.await_args.kwargs == {"title": "音乐渲染"}

        text = h.text
        assert f"[乐谱文件已保存到沙盒: {sandbox_path}]" in text
        assert "[已自动提交音乐渲染任务 thread_id=t-music" in text
        assert "[系统通知] 已启用音乐技能" in text
        assert SKILL in text
        assert len(h.queued) == 1
    finally:
        await _close(h)


@pytest.mark.parametrize("name", ["a.midi", "b.MID", "c.musicxml", "d.xml", "e.mxl"])
async def test_every_supported_extension_is_staged(harnesses: Any, name: str) -> None:
    payloads = {
        "mxl": b"PK\x03\x04zip",
        "xml": b"<?xml?><x/>",
        "musicxml": b"<?xml?><x/>",
    }
    ext = name.rsplit(".", 1)[1].lower()
    data = payloads.get(ext, MIDI)
    h: Harness = harnesses(response={"data": {"base64": base64.b64encode(data).decode()}})
    try:
        await h.app.handle_message(_private(Segment("text", {"text": "x"}), _file(name)))
        files = await h.inbox()
        assert len(files) == 1
        assert files[0].suffix == f".{ext}"
        assert files[0].read_bytes() == data
    finally:
        await _close(h)


async def test_auto_render_off_stages_and_notifies_but_does_not_submit(harnesses: Any) -> None:
    h: Harness = harnesses("[music]\nenabled = true\nauto_render_files = false\n")
    try:
        await h.app.handle_message(_private(_file()))
        assert len(await h.inbox()) == 1
        h.submit.assert_not_awaited()
        assert "[乐谱文件已保存到沙盒:" in h.text
        assert "自动提交" not in h.text
        assert "[系统通知] 已启用音乐技能" in h.text
    finally:
        await _close(h)


async def test_submit_failure_is_soft(harnesses: Any) -> None:
    h: Harness = harnesses()
    h.submit.side_effect = RuntimeError("secret-detail")
    try:
        await h.app.handle_message(_private(_file()))
        assert "[自动渲染任务提交失败" in h.text
        assert "secret-detail" not in h.text
        assert len(await h.inbox()) == 1
    finally:
        await _close(h)


async def test_oversized_file_declared_size_is_refused_without_fetch(harnesses: Any) -> None:
    h: Harness = harnesses("[music]\nenabled = true\nmax_file_bytes = 100\n")
    try:
        await h.app.handle_message(_private(_file(file_size="101")))
        assert h.napcat_calls == []
        assert await h.inbox() == []
        h.submit.assert_not_awaited()
        assert "[乐谱文件未能保存: too_large]" in h.text
    finally:
        await _close(h)


async def test_wrong_content_is_refused(harnesses: Any) -> None:
    h: Harness = harnesses(response={"data": {"base64": base64.b64encode(b"nope").decode()}})
    try:
        await h.app.handle_message(_private(_file()))
        assert await h.inbox() == []
        h.submit.assert_not_awaited()
        assert "[乐谱文件未能保存: unsupported_score]" in h.text
    finally:
        await _close(h)


async def test_napcat_failure_is_soft_and_message_still_delivered(
    harnesses: Any, caplog: pytest.LogCaptureFixture
) -> None:
    h: Harness = harnesses(response=TimeoutError("https://secret.example/x?token=abc"))
    try:
        await h.app.handle_message(_private(_file()))
        assert len(h.queued) == 1
        assert "[乐谱文件未能保存: fetch_failed]" in h.text
        assert "secret.example" not in caplog.text
        assert "token=abc" not in h.text
    finally:
        await _close(h)


async def test_non_score_files_are_left_alone(harnesses: Any) -> None:
    h: Harness = harnesses()
    try:
        await h.app.handle_message(_private(_file("report.pdf")))
        assert h.napcat_calls == []
        h.submit.assert_not_awaited()
        assert h.text == "[文件]"
    finally:
        await _close(h)


async def test_too_many_scores_in_one_message_are_capped(harnesses: Any) -> None:
    h: Harness = harnesses()
    try:
        await h.app.handle_message(_private(*(_file(f"s{i}.mid") for i in range(5))))
        assert len(await h.inbox()) == 3
        assert "其余未保存" in h.text
        assert h.submit.await_count == 1
    finally:
        await _close(h)


async def test_keyword_text_injects_once_per_window(harnesses: Any) -> None:
    h: Harness = harnesses()
    try:
        await h.app.handle_message(_private(Segment("text", {"text": "能把 MIDI 合成音频吗"})))
        assert SKILL in h.text
        await h.app.handle_message(_private(Segment("text", {"text": "再来一首 midi"})))
        assert SKILL not in h.text
        await h.app.handle_message(_private(Segment("text", {"text": "你好"})))
        assert h.text == "你好"
        assert h.submit.await_count == 0  # 只有文件才自动提交
    finally:
        await _close(h)


async def test_plain_text_never_injects(harnesses: Any) -> None:
    h: Harness = harnesses()
    try:
        await h.app.handle_message(_private(Segment("text", {"text": "今天吃什么"})))
        assert h.text == "今天吃什么"
    finally:
        await _close(h)


async def test_music_disabled_is_inert(harnesses: Any) -> None:
    h: Harness = harnesses("")
    try:
        assert h.app._music_skill is None
        assert h.app._score_stager is None
        assert h.app._coding is not None
        assert h.app._coding._sandbox._soundfont_dir is None
        assert h.app._coding._sandbox._muse_dir is None
        await h.app.handle_message(_private(Segment("text", {"text": "midi 乐谱"}), _file()))
        assert h.napcat_calls == []
        h.submit.assert_not_awaited()
        assert h.text == "midi 乐谱[文件]"
        assert "音乐" not in await h.app._private_instructions("private:1")
    finally:
        await _close(h)


async def test_music_without_coding_has_no_staging_or_injection(harnesses: Any) -> None:
    h: Harness = harnesses(coding=False)
    try:
        assert h.app._music_skill is None
        assert h.app._score_stager is None
        await h.app.handle_message(_private(Segment("text", {"text": "midi"}), _file()))
        assert h.text == "midi[文件]"
    finally:
        await _close(h)


async def test_enabled_wires_mount_prompt_and_wrapped_tools(harnesses: Any) -> None:
    h: Harness = harnesses()
    try:
        assert h.app._coding is not None
        assert h.app._coding._sandbox._soundfont_dir == h.config.music.soundfont_dir.absolute()
        assert h.app._coding._sandbox._muse_dir is None
        instructions = await h.app._private_instructions("private:1")
        assert MUSIC_PROMPT in instructions
        for name in ("codex_submit", "codex_steer", "codex_list"):
            assert h.app._runner._tools.get(name) is not None
    finally:
        await _close(h)


async def test_configured_muse_dir_reaches_sandbox(harnesses: Any, tmp_path: Path) -> None:
    h: Harness = harnesses('[music]\nenabled = true\nmuse_dir = "muse"\n')
    try:
        assert h.app._coding is not None
        assert h.app._coding._sandbox._muse_dir == tmp_path / "muse"
    finally:
        await _close(h)


# ---------------------------------------------------------------- 群聊从不注入


class _Forbidden:
    """替换注入器方法:被调用即失败。"""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"group path touched the music injector: {name}")


async def test_group_message_never_stages_fetches_or_injects(
    harnesses: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    h: Harness = harnesses()
    try:
        monkeypatch.setattr(h.app, "_music_skill", _Forbidden())
        message = _group(Segment("text", {"text": "合成这个 midi musescore"}), _file())
        # 真实群路径:Bot QQ 号未知,消息在门控前被丢弃;任何触碰注入器/NapCat/沙盒都会失败。
        await h.app.handle_message(message)
        assert h.napcat_calls == []
        assert await h.inbox() == []
        h.submit.assert_not_awaited()
        assert h.queued == []
    finally:
        await _close(h)


async def test_group_message_routed_to_group_handler_only(
    harnesses: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    h: Harness = harnesses()
    try:
        monkeypatch.setattr(h.app, "_music_skill", _Forbidden())
        group_handler = AsyncMock()
        monkeypatch.setattr(h.app, "_handle_group", group_handler)
        message = _group(Segment("text", {"text": "音源 soundfont 乐谱"}), _file())
        await h.app.handle_message(message)
        group_handler.assert_awaited_once_with(message)
        assert h.napcat_calls == []
        h.submit.assert_not_awaited()
        assert h.queued == []
    finally:
        await _close(h)


async def test_group_session_manager_has_no_coding_tools_or_injector(harnesses: Any) -> None:
    h: Harness = harnesses()
    try:
        # 群路径只持有群会话管理器;它的构造参数里没有 codex 工具注册表或注入器。
        groups = h.app._groups
        assert not any(value is h.app._music_skill for value in vars(groups).values())
        registry = getattr(groups, "_tools", None)
        if registry is not None:
            assert registry.get("codex_submit") is None
    finally:
        await _close(h)
