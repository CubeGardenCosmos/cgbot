"""Session rows -> Responses input items (R1.6).

Row layout written by ``AgentRunner`` (``payload`` keys):
- ``user`` / ``system_event``: ``item`` = the input item sent with the turn. ``system_event``
  rows without ``item`` are observations (e.g. model failures) and are not model input.
- ``assistant``: ``item`` = model output item kept as pass-through (text, reasoning, other);
  ``internal`` is always true: model text is never sent to a user.
- ``tool_call``: ``item`` = the ``function_call`` input item; ``call_id``, ``name``.
- ``tool_result``: ``call_id``, ``output``, ``is_error``.
- ``compaction``: ``summary``; the active window starts here (see ``SessionStore.load_active``).
  ``retained`` (optional) = verbatim copies of the rows kept after the summary, each a dict
  with ``seq``, ``kind``, ``payload``, ``ts``; see ``expand_window``.

Pairing: a ``function_call`` is emitted only with a matching ``function_call_output``
(synthesised by the runner for unanswered calls), and an output is emitted only if its call is
in the active window. The first output per ``call_id`` wins.
"""

from __future__ import annotations

import copy
from collections.abc import Sequence
from typing import Any, cast

from cgbot.provider import function_call_output, user_message
from cgbot.session import Item, Kind

COMPACTION_PREFIX = "以下是此前对话的压缩摘要,供延续上下文:\n"
CANCELLED_OUTPUT = (
    "cancelled: the turn was stopped before this tool returned; no result was produced."
)
INTERRUPTED_OUTPUT = (
    "interrupted: the previous run ended before this tool returned; no result was produced."
)

_ITEM_KINDS = frozenset({"user", "system_event", "assistant", "tool_call"})


def expand_window(items: Sequence[Item]) -> list[Item]:
    """Active window with each compaction's retained tail spliced in after it.

    The retained rows are virtual copies (their seq is the original row's seq); they are
    never stored twice. A window contains at most one compaction, at its head.
    """
    out: list[Item] = []
    for item in items:
        out.append(item)
        if item.kind != "compaction":
            continue
        for row in item.payload.get("retained", []):
            out.append(
                Item(
                    seq=int(row["seq"]),
                    session_id=item.session_id,
                    kind=cast(Kind, row["kind"]),
                    payload=dict(row["payload"]),
                    ts=float(row["ts"]),
                )
            )
    return out


def to_input_items(items: Sequence[Item]) -> list[dict[str, Any]]:
    """Convert an active window (``load_active`` order) into Responses input items."""
    return render_items(expand_window(items))


def render_items(expanded: Sequence[Item]) -> list[dict[str, Any]]:
    """Render an already expanded window (see ``expand_window``) into input items."""
    called = {str(i.payload["call_id"]) for i in expanded if i.kind == "tool_call"}
    answered: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in expanded:
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
