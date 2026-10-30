"""Pure compaction rules (no I/O).

- Threshold: compaction is due when context tokens reach 50% of ``provider.context_window``.
- Tail: the last ``keep_exchanges`` user exchanges stay verbatim. The cut is moved forward
  until no tool call straddles it (a call before the cut whose result lies at or after it)
  and no unanswered call lies in the tail.
- Estimate: UTF-8 bytes / 3 of the rendered input (conservative for English, close for CJK).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from cgbot.session import Item

KEEP_EXCHANGES = 3
IDLE_SECONDS = 8 * 3600.0
BYTES_PER_TOKEN = 3
PRIVATE_PREFIX = "private:"

DEFAULT_SUMMARY_INSTRUCTIONS = (
    "你是对话压缩器。阅读给出的对话记录,写出一份供之后继续同一段对话使用的摘要。"
    "保留:用户的身份信息与偏好、已确认的事实与决定、未完成的事项与约定、工具调用的关键结果。"
    "省略寒暄与重复内容。只输出摘要正文,不要提问,不要调用工具。"
)
SUMMARY_REQUEST = "请输出上述对话的摘要。"


def estimate_tokens(input_items: Sequence[dict[str, Any]]) -> int:
    """Conservative token estimate of rendered input items (UTF-8 bytes / BYTES_PER_TOKEN)."""
    text = json.dumps(list(input_items), ensure_ascii=False, separators=(",", ":"), default=str)
    return -(-len(text.encode("utf-8")) // BYTES_PER_TOKEN)


def over_threshold(tokens: int, context_window: int) -> bool:
    """True when ``tokens`` is at least 50% of ``context_window`` (integer arithmetic)."""
    return tokens * 2 >= context_window


def starts_exchange(item: Item) -> bool:
    """A user message, or a system event that is model input, begins an exchange."""
    return item.kind == "user" or (item.kind == "system_event" and "item" in item.payload)


def _call_constraints(expanded: Sequence[Item]) -> tuple[list[tuple[int, int]], int]:
    """Forbidden cut intervals ``(a, b)`` (cut ``i`` with ``a < i <= b`` splits a pair) and
    the highest index of an unanswered call (every cut must lie after it)."""
    call_at: dict[str, int] = {}
    result_at: dict[str, int] = {}
    for idx, item in enumerate(expanded):
        if item.kind == "tool_call":
            call_at.setdefault(str(item.payload["call_id"]), idx)
        elif item.kind == "tool_result":
            result_at.setdefault(str(item.payload["call_id"]), idx)
    intervals: list[tuple[int, int]] = []
    last_unanswered = -1
    for call_id, a in call_at.items():
        b = result_at.get(call_id)
        if b is None:
            last_unanswered = max(last_unanswered, a)
        elif a < b:
            intervals.append((a, b))
    return intervals, last_unanswered


def choose_cut(expanded: Sequence[Item], keep_exchanges: int) -> int:
    """Index at which the retained tail starts; ``expanded[:cut]`` is summarised.

    Desired start: the ``keep_exchanges``-th exchange from the end (or the first exchange when
    there are fewer). Moved forward to the first index that splits no tool call. Returns 0 when
    nothing precedes the desired start. Index ``len(expanded)`` is always valid (empty tail).
    """
    n = len(expanded)
    starts = [i for i, item in enumerate(expanded) if starts_exchange(item)]
    if not starts:
        desired = n
    elif len(starts) >= keep_exchanges:
        desired = starts[-keep_exchanges]
    else:
        desired = starts[0]
    intervals, last_unanswered = _call_constraints(expanded)
    for cut in range(desired, n + 1):
        if cut <= last_unanswered:
            continue
        if any(a < cut <= b for a, b in intervals):
            continue
        return cut
    return n  # unreachable: n is valid, kept for type clarity


_ROLE_LABELS = {"user": "用户", "system_event": "系统事件", "assistant": "助手"}


def _message_text(item: dict[str, Any]) -> str | None:
    kind = item.get("type")
    if kind == "message":
        parts = item.get("content")
        if isinstance(parts, str):
            return parts
        if isinstance(parts, list):
            texts = [str(p["text"]) for p in parts if isinstance(p, dict) and "text" in p]
            return "".join(texts) if texts else None
        return None
    if kind == "function_call_output":
        return str(item.get("output", ""))
    return None


def _transcript_line(item: Item) -> str | None:
    payload = item.payload
    if item.kind == "compaction":
        return f"[此前摘要] {payload['summary']}"
    if item.kind == "tool_call":
        return f"[工具调用 {payload.get('name', '')}] {payload.get('arguments', '')}"
    if item.kind == "tool_result":
        return f"[工具结果 {payload.get('name', '')}] {payload['output']}"
    if item.kind in _ROLE_LABELS and "item" in payload:
        text = _message_text(dict(payload["item"]))
        if text:
            return f"{_ROLE_LABELS[item.kind]}: {text}"
    return None


def transcript(items: Sequence[Item]) -> str:
    """Plain-text rendering of a window for the summary request (reasoning is omitted)."""
    lines = [line for item in items if (line := _transcript_line(item)) is not None]
    return "\n".join(lines)
