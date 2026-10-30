"""Plain-text helpers for group gating and the unhandled-message buffer. Pure, no I/O."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from cgbot.model import Segment

_PLACEHOLDERS: dict[str, str] = {
    "image": "[图片]",
    "face": "[表情]",
    "record": "[语音]",
    "video": "[视频]",
    "file": "[文件]",
    "forward": "[转发]",
}


def keyword_text(segments: Sequence[Segment]) -> str:
    """Concatenated ``text`` segments only; @-targets and placeholders never match keywords."""
    parts: list[str] = []
    for seg in segments:
        if seg.type == "text":
            value = seg.data.get("text")
            if isinstance(value, str):
                parts.append(value)
    return "".join(parts)


def plain_text(segments: Sequence[Segment]) -> str:
    """Human-readable rendering for buffered context: text, ``@qq`` and media placeholders."""
    parts: list[str] = []
    for seg in segments:
        if seg.type == "text":
            value = seg.data.get("text")
            if isinstance(value, str):
                parts.append(value)
        elif seg.type == "at":
            parts.append(f"@{seg.data.get('qq', '')}")
        elif seg.type in _PLACEHOLDERS:
            parts.append(_PLACEHOLDERS[seg.type])
    return "".join(parts)


def contains_keyword(text: str, keywords: Iterable[str]) -> bool:
    """Case-insensitive substring match. Empty keywords never match."""
    folded = text.casefold()
    return any(keyword and keyword.casefold() in folded for keyword in keywords)


def mentions_user(segments: Sequence[Segment], user_id: str) -> bool:
    """True when an ``at`` segment targets ``user_id``."""
    return any(seg.type == "at" and str(seg.data.get("qq", "")) == user_id for seg in segments)
