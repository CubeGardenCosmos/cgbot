"""音乐技能注入:关键词、TTL 窗口、仅私聊、codex_submit/codex_steer 结果包装。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from cgbot.agent.tools import ToolContext, ToolResult, ToolSpec
from cgbot.coding.contracts import CodingService, SubmitResult
from cgbot.coding.tools import codex_tools
from cgbot.model import Channel, OutboundMessage, SendResult
from cgbot.music.skill import (
    NO_MUSESCORE_NOTE,
    SkillInjector,
    build_injector,
    load_skill_text,
    music_keywords_match,
    render_prompt,
    skill_body,
    with_music_skill,
)

TEXT = "SKILL-BODY cgmusic cgdl"
PRIVATE = "private:u1"
GROUP = "group:2001"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _injector(clock: Clock | None = None, **kwargs: Any) -> SkillInjector:
    return SkillInjector(TEXT, 100.0, clock=clock or Clock(), **kwargs)


@pytest.mark.parametrize(
    "text",
    [
        "帮我把这个 MIDI 合成音频",
        "这首曲子的 musicxml 能转音频吗",
        "能渲染音频吗",
        "去 MuseScore 找一下谱",
        "换个 soundfont 试试",
        "把乐谱变成 mp3",
        "I need to render audio from sheet music",
        "用 sf3 音源",
    ],
)
def test_keywords_match(text: str) -> None:
    assert music_keywords_match(text)


@pytest.mark.parametrize("text", ["你好", "今天天气不错", "帮我做一段语音合成", "考试分数是多少"])
def test_keywords_do_not_match_unrelated(text: str) -> None:
    assert not music_keywords_match(text)


def test_notice_once_per_window_then_again_after_ttl() -> None:
    clock = Clock()
    injector = _injector(clock)
    first = injector.notice_for_text(PRIVATE, "合成这个 midi")
    assert first is not None
    assert TEXT in first
    assert first.startswith("[系统通知]")
    assert injector.notice_for_text(PRIVATE, "再来一个 midi") is None
    assert injector.for_tool(PRIVATE, "render midi") is None
    clock.now += 101
    again = injector.notice_for_text(PRIVATE, "再来一个 midi")
    assert again is not None
    assert TEXT in again


def test_window_is_not_extended_by_activity() -> None:
    clock = Clock()
    injector = _injector(clock)
    assert injector.notice_for_text(PRIVATE, "midi") is not None
    clock.now += 90
    assert injector.notice_for_text(PRIVATE, "midi") is None
    clock.now += 20  # 总共 110 > 100:窗口从首次激活起算
    assert injector.notice_for_text(PRIVATE, "midi") is not None


def test_sessions_are_independent() -> None:
    injector = _injector()
    assert injector.notice_for_text("private:a", "midi") is not None
    assert injector.notice_for_text("private:b", "midi") is not None


def test_text_without_keywords_does_not_inject() -> None:
    injector = _injector()
    assert injector.notice_for_text(PRIVATE, "你好") is None
    assert injector.for_tool(PRIVATE, "查一下天气") is None
    assert injector.claim(PRIVATE) is None


def test_force_activates_without_keywords() -> None:
    injector = _injector()
    assert injector.notice_for_text(PRIVATE, "[文件]", force=True) is not None


@pytest.mark.parametrize("session", [GROUP, "group:1", "", "x", "privatex:1"])
def test_non_private_session_never_injects(session: str) -> None:
    injector = _injector()
    assert injector.activate(session) is False
    assert injector.observe(session, "midi musescore 合成音频") is False
    assert injector.claim(session) is None
    assert injector.notice_for_text(session, "midi", force=True) is None
    assert injector.for_tool(session, "midi") is None


def test_musescore_note_when_disabled() -> None:
    injector = _injector(musescore_enabled=False)
    notice = injector.notice_for_text(PRIVATE, "midi")
    assert notice is not None
    assert NO_MUSESCORE_NOTE in notice
    plain = _injector().notice_for_text(PRIVATE, "midi")
    assert plain is not None
    assert NO_MUSESCORE_NOTE not in plain


def test_invalid_ttl_rejected() -> None:
    with pytest.raises(ValueError):
        SkillInjector(TEXT, 0)


def test_skill_body_strips_frontmatter() -> None:
    assert skill_body("---\nname: x\ndescription: y\n---\n\n# Title\nbody\n") == "# Title\nbody"
    assert skill_body("no frontmatter") == "no frontmatter"


def test_real_skill_file_is_the_single_source() -> None:
    text = load_skill_text()
    repo = Path(__file__).resolve().parents[2]
    raw = (repo / "sandbox" / "skills" / "music-synth" / "SKILL.md").read_text(encoding="utf-8")
    assert raw.startswith("---\nname: music-synth\n")
    assert text == skill_body(raw)
    assert not text.startswith("---")
    for needle in ("cgmusic", "cgdl", "/opt/soundfonts", "report_progress", "lufs", "Musescore"):
        assert needle in text


def test_load_skill_text_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_skill_text(tmp_path / "absent.md")


def test_build_injector_off_and_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from music_fakes import music_config

    assert build_injector(music_config(tmp_path).music) is None
    enabled = music_config(tmp_path, "[music]\nenabled = true\n").music
    assert build_injector(enabled) is not None
    monkeypatch.setattr("cgbot.music.skill._REPO_SKILL", tmp_path / "none.md")
    monkeypatch.setattr("cgbot.music.skill._IMAGE_SKILL", tmp_path / "none2.md")
    assert build_injector(enabled) is None


def test_render_prompt_mentions_paths_and_delivery() -> None:
    prompt = render_prompt(["/workspace/inbox/score-1.mid", "/workspace/inbox/score-2.mxl"])
    assert "cgmusic render" in prompt
    assert "report_progress" in prompt
    assert "/workspace/inbox/score-1.mid" in prompt
    assert "/workspace/inbox/score-2.mxl" in prompt


# ---------------------------------------------------------------- tool wrapping


class _Service:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def submit(
        self,
        user_id: str,
        prompt: str,
        *,
        thread_id: str | None = None,
        title: str | None = None,
        important: bool = False,
        time_limit_minutes: float | None = None,
    ) -> SubmitResult:
        self.calls.append(prompt)
        return SubmitResult(thread_id="t1", state="started", queue_position=0)

    async def steer(self, user_id: str, thread_id: str, text: str) -> None:
        self.calls.append(text)


def _ctx(session_id: str) -> ToolContext:
    async def sender(message: OutboundMessage) -> SendResult:
        return SendResult(ok=True)

    channel = Channel(kind="qq", account="1001")
    return ToolContext(
        user_id="u1",
        session_id=session_id,
        source_channel=channel,
        source_message_id=None,
        sender=sender,
        channels=(channel,),
    )


def _tools(injector: SkillInjector) -> dict[str, ToolSpec]:
    specs = codex_tools(cast(CodingService, _Service()))
    return {spec.name: spec for spec in with_music_skill(specs, injector)}


async def test_submit_and_steer_results_carry_skill_once() -> None:
    tools = _tools(_injector())
    first = await tools["codex_submit"].handler(_ctx(PRIVATE), {"prompt": "合成这个 midi"})
    assert not first.is_error
    assert "已提交 thread_id=t1" in first.output
    assert TEXT in first.output
    second = await tools["codex_steer"].handler(
        _ctx(PRIVATE), {"thread_id": "t1", "text": "换成钢琴 soundfont"}
    )
    assert "已把指示追加" in second.output
    assert TEXT not in second.output  # 同一窗口只注入一次


async def test_steer_alone_injects_when_text_matches() -> None:
    tools = _tools(_injector())
    result = await tools["codex_steer"].handler(
        _ctx(PRIVATE), {"thread_id": "t1", "text": "改用 sf3 音源"}
    )
    assert TEXT in result.output


async def test_window_opened_by_user_text_reaches_unrelated_submit_prompt() -> None:
    injector = _injector()
    assert injector.notice_for_text(PRIVATE, "我发了乐谱") is not None
    tools = _tools(injector)
    result = await tools["codex_submit"].handler(_ctx(PRIVATE), {"prompt": "处理一下"})
    assert TEXT not in result.output  # 通知项已经注入过


async def test_open_window_without_prior_delivery_injects_into_tool_result() -> None:
    injector = _injector()
    injector.activate(PRIVATE)  # 窗口已开但尚未注入
    tools = _tools(injector)
    result = await tools["codex_submit"].handler(_ctx(PRIVATE), {"prompt": "处理一下"})
    assert TEXT in result.output


async def test_unrelated_submit_is_untouched() -> None:
    tools = _tools(_injector())
    result = await tools["codex_submit"].handler(_ctx(PRIVATE), {"prompt": "查一下天气"})
    assert result.output == "已提交 thread_id=t1，已开始。结果稍后以通知送达。"


async def test_group_session_never_injected_through_tools() -> None:
    injector = _injector()
    tools = _tools(injector)
    result = await tools["codex_submit"].handler(_ctx(GROUP), {"prompt": "合成 midi 音频"})
    assert result.is_error
    assert TEXT not in result.output
    assert injector.claim(GROUP) is None
    assert injector.claim(PRIVATE) is None  # 群聊调用也不会为私聊开窗口


async def test_error_results_are_not_decorated() -> None:
    injector = _injector()
    tools = _tools(injector)
    result = await tools["codex_submit"].handler(_ctx(PRIVATE), {"prompt": "midi", "bogus": 1})
    assert result.is_error
    assert TEXT not in result.output


def test_other_tools_are_passed_through_unchanged() -> None:
    specs = codex_tools(cast(CodingService, _Service()))
    wrapped = with_music_skill(specs, _injector())
    by_old = {s.name: s for s in specs}
    for spec in wrapped:
        if spec.name not in {"codex_submit", "codex_steer"}:
            assert spec is by_old[spec.name]
        else:
            assert spec.parameters_schema == by_old[spec.name].parameters_schema
    assert [s.name for s in wrapped] == [s.name for s in specs]


async def test_non_string_argument_is_safe() -> None:
    injector = _injector()

    async def inner(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        return ToolResult("ok")

    spec = ToolSpec("codex_submit", "d", {"type": "object"}, inner)
    (wrapped,) = with_music_skill([spec], injector)
    assert (await wrapped.handler(_ctx(PRIVATE), {"prompt": 5})).output == "ok"
