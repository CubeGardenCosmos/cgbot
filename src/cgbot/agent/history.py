"""Session rows -> Responses input items (R1.6).

Row layout written by ``AgentRunner`` (``payload`` keys):
- ``user`` / ``system_event``: ``item`` = the input item sent with the turn. ``system_event``
  rows without ``item`` are observations (e.g. model failures) and are not model input.
- ``assistant``: ``item`` = model output item kept as pass-through (text, reasoning, other);
  ``internal`` is always true: model text is never sent to a user.
- ``tool_call``: ``item`` = the ``function_call`` input item; ``call_id``, ``name``.
- ``tool_result``: ``call_id``, ``output``, ``is_error``.
- ``compaction``: ``summary``; the active window starts here (see ``SessionStore.load_active``).

Pairing: a ``function_call`` is emitted only with a matching ``function_call_output``
(synthesised by the runner for unanswered calls), and an output is emitted only if its call is
in the active window. The first output per ``call_id`` wins.
"""

from __future__ import annotations

import copy
from collections.abc import Sequence
from typing import Any

from cgbot.provider import function_call_output, user_message
from cgbot.session import Item

COMPACTION_PREFIX = "以下是此前对话的压缩摘要,供延续上下文:\n"
CANCELLED_OUTPUT = (
    "cancelled: the turn was stopped before this tool returned; no result was produced."
)
INTERRUPTED_OUTPUT = (
    "interrupted: the previous run ended before this tool returned; no result was produced."
)

_ITEM_KINDS = frozenset({"user", "system_event", "assistant", "tool_call"})


def to_input_items(items: Sequence[Item]) -> list[dict[str, Any]]:
    """Convert an active window (``load_active`` order) into Responses input items."""
    called = {str(i.payload["call_id"]) for i in items if i.kind == "tool_call"}
    answered: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in items:
        payload = item.payload
        if item.kind == "compaction":
            out.append(user_message(COMPACTION_PREFIX + str(payload["summary"])))
        elif item.kind in _ITEM_KINDS:
            if "item" in payload:
                out.append(copy.deepcopy(dict(payload["item"])))
        elif item.kind == "tool_result":
            call_id = str(payload["call_id"])
            if call_id in called and call_id not in answered:
                answered.add(call_id)
                out.append(function_call_output(call_id, str(payload["output"])))
    return out


def unanswered_calls(items: Sequence[Item]) -> list[tuple[str, str]]:
    """``(call_id, name)`` of tool calls in the window that have no result row, in order."""
    answered = {str(i.payload["call_id"]) for i in items if i.kind == "tool_result"}
    return [
        (str(i.payload["call_id"]), str(i.payload.get("name", "")))
        for i in items
        if i.kind == "tool_call" and str(i.payload["call_id"]) not in answered
    ]
