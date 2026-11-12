"""音乐技能注入:关键词(中英文)触发,仅私聊,每个 TTL 窗口每个会话至多注入一次。

技能正文只有一个来源:``sandbox/skills/music-synth/SKILL.md``(沙盒内 Codex 读同一份)。
注入路径有二:私聊用户文本/乐谱文件触发的通知项,以及 ``codex_submit``/``codex_steer`` 的工具结果。
群聊既不构造也不查询注入器:群会话没有 ``codex_*`` 工具,且 ``SkillInjector`` 对非私聊会话 id
一律拒绝(见 tests/music/test_skill.py)。
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, Final

from cgbot.agent.tools import ToolContext, ToolHandler, ToolResult, ToolSpec
from cgbot.config.settings import MusicConfig
from cgbot.memory.domain import PRIVATE_SESSION_PREFIX

log = logging.getLogger(__name__)

SKILL_PATH_ENV: Final = "CGBOT_MUSIC_SKILL"
# 仓库内路径(开发/测试)与 bot 镜像内路径(Dockerfile 复制)。
_REPO_SKILL: Final = (
    Path(__file__).resolve().parents[3] / "sandbox" / "skills" / "music-synth" / "SKILL.md"
)
_IMAGE_SKILL: Final = Path("/app/skills/music-synth/SKILL.md")

INJECTED_TOOLS: Final = frozenset({"codex_submit", "codex_steer"})
NOTICE_HEADER: Final = "[系统通知] 已启用音乐技能(本窗口内交办音乐任务时按下文使用 cgmusic / cgdl):"
TOOL_HEADER: Final = "[音乐技能] 交办音乐合成任务时请遵循:"
NO_MUSESCORE_NOTE: Final = (
    "注意:本部署未启用 Musescore(cgdl musescore-*),不要搜索或下载 Musescore 乐谱。"
)
MUSESCORE_TOOL_NOTE: Final = (
    "Musescore(仅私聊):找乐谱先用 musescore_search(只返回社区乐谱,不含官方)。"
    "交办任务时把选定乐谱的完整 url 写入任务提示,"
    "沙盒内用 cgdl musescore-get <url> --kind mid|mxl|mp3|pdf 获取。"
    "用户想先看乐谱样子时用 musescore_preview 把第 1 页图片发给用户(只发图,不下载)。"
)

_FRONTMATTER: Final = re.compile(r"\A---\r?\n.*?\r?\n---\r?\n", re.DOTALL)
_KEYWORDS: Final = (
    # English
    "midi",
    ".mid",
    "musicxml",
    ".mxl",
    "soundfont",
    "sf2",
    "sf3",
    "musescore",
    "fluidsynth",
    "cgmusic",
    "sheet music",
    "render audio",
    "synthesize music",
    # 中文(不单用"合成",避免与语音合成混淆)
    "音源",
    "音色库",
    "乐谱",
    "谱面",
    "曲谱",
    "五线谱",
    "钢琴谱",
    "简谱",
    "转音频",
    "渲染音频",
    "音频合成",
    "合成音乐",
    "合成音频",
    "乐谱合成",
    "合成成音频",
)


def skill_body(raw: str) -> str:
    """去掉 YAML frontmatter,返回正文。"""
    return _FRONTMATTER.sub("", raw, count=1).strip()


def load_skill_text(path: Path | None = None) -> str:
    """读取技能正文。``path`` 缺省依次尝试环境变量、仓库路径、镜像路径;都没有则抛 OSError。"""
    candidates: Iterable[Path]
    if path is not None:
        candidates = (path,)
    else:
        override = os.environ.get(SKILL_PATH_ENV)
        candidates = (*((Path(override),) if override else ()), _REPO_SKILL, _IMAGE_SKILL)
    for candidate in candidates:
        if candidate.is_file():
            text = skill_body(candidate.read_text(encoding="utf-8"))
            if text:
                return text
    raise FileNotFoundError("music-synth SKILL.md not found")


def music_keywords_match(text: str) -> bool:
    folded = text.casefold()
    return any(keyword in folded for keyword in _KEYWORDS)


def is_private_session(session_id: str) -> bool:
    return session_id.startswith(PRIVATE_SESSION_PREFIX)


class SkillInjector:
    """会话级 TTL 窗口;状态只在内存,私聊会话 id 之外一律拒绝。

    窗口从首次激活起算固定 ``ttl_seconds``(不续期);窗口内 :meth:`claim` 至多返回一次技能文本,
    窗口过期后再次激活才开启新窗口并允许再次注入。
    """

    def __init__(
        self,
        text: str,
        ttl_seconds: float = 3600.0,
        *,
        musescore_enabled: bool = True,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("skill TTL must be positive")
        note = MUSESCORE_TOOL_NOTE if musescore_enabled else NO_MUSESCORE_NOTE
        self._text = f"{text}\n\n{note}"
        self._ttl = ttl_seconds
        self._clock = clock
        self._expires: dict[str, float] = {}
        self._delivered: set[str] = set()

    def activate(self, session_id: str) -> bool:
        """打开私聊窗口(已有未过期窗口则不变)。非私聊返回 False。"""
        if not is_private_session(session_id):
            return False
        now = self._clock()
        if self._expires.get(session_id, float("-inf")) <= now:
            self._expires[session_id] = now + self._ttl
            self._delivered.discard(session_id)
        return True

    def observe(self, session_id: str, text: str | None) -> bool:
        """文本命中关键词则激活窗口;返回窗口当前是否有效。"""
        if not is_private_session(session_id):
            return False
        if text is not None and music_keywords_match(text):
            self.activate(session_id)
        return self._active(session_id)

    def claim(self, session_id: str) -> str | None:
        """窗口有效且尚未注入时返回技能文本并标记已注入;否则 None。"""
        if not self._active(session_id) or session_id in self._delivered:
            return None
        self._delivered.add(session_id)
        return self._text

    def notice_for_text(self, session_id: str, text: str, *, force: bool = False) -> str | None:
        """私聊用户文本(或收到乐谱文件时 ``force``)触发的通知项正文。"""
        if not is_private_session(session_id):
            return None
        if force:
            self.activate(session_id)
        elif not self.observe(session_id, text):
            return None
        body = self.claim(session_id)
        return None if body is None else f"{NOTICE_HEADER}\n\n{body}"

    def for_tool(self, session_id: str, prompt: str | None) -> str | None:
        """``codex_submit``/``codex_steer`` 工具结果的附加正文(提示词命中关键词也会激活)。"""
        if not self.observe(session_id, prompt):
            return None
        body = self.claim(session_id)
        return None if body is None else f"{TOOL_HEADER}\n\n{body}"

    def _active(self, session_id: str) -> bool:
        expires = self._expires.get(session_id)
        if expires is None:
            return False
        if expires <= self._clock():
            self._expires.pop(session_id, None)
            self._delivered.discard(session_id)
            return False
        return True


def _wrap(spec: ToolSpec, injector: SkillInjector) -> ToolSpec:
    inner: ToolHandler = spec.handler
    argument = "prompt" if spec.name == "codex_submit" else "text"

    async def handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        result = await inner(ctx, args)
        if result.is_error or not is_private_session(ctx.session_id):
            return result
        text = args.get(argument)
        extra = injector.for_tool(ctx.session_id, text if isinstance(text, str) else None)
        if extra is None:
            return result
        return ToolResult(output=f"{result.output}\n\n{extra}", is_error=False)

    return ToolSpec(
        name=spec.name,
        description=spec.description,
        parameters_schema=spec.parameters_schema,
        handler=handler,
        channel_param=spec.channel_param,
    )


def with_music_skill(specs: Iterable[ToolSpec], injector: SkillInjector) -> tuple[ToolSpec, ...]:
    """包装 ``codex_submit``/``codex_steer``,其余工具原样返回。"""
    return tuple(_wrap(s, injector) if s.name in INJECTED_TOOLS else s for s in specs)


def build_injector(config: MusicConfig) -> SkillInjector | None:
    """按 ``[music]`` 构造注入器;关闭或技能文件缺失(只记警告)时返回 None。"""
    if not config.enabled:
        return None
    try:
        text = load_skill_text()
    except OSError as exc:
        log.warning("music skill disabled, SKILL.md unavailable: error=%s", type(exc).__name__)
        return None
    return SkillInjector(text, config.skill_ttl_seconds, musescore_enabled=config.musescore_enabled)


def render_prompt(paths: Iterable[str]) -> str:
    """收到乐谱文件后自动提交的 Codex 渲染任务提示词。"""
    listing = "\n".join(f"- {path}" for path in paths)
    return (
        "用户发来了乐谱文件,已保存在沙盒:\n"
        f"{listing}\n"
        "请按 music-synth 技能,对每个文件用 `cgmusic render <文件>` 合成音频"
        "(先用 `cgmusic soundfonts` 查看可用音源,通常用默认音源即可),"
        "然后用 report_progress 的 files 参数把生成的音频文件发给用户,"
        "并用一两句话报告时长与综合响度(LUFS)。只渲染一次,不要反复重渲染;失败就如实说明原因。"
    )
