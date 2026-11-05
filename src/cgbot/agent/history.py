"""Session rows -> Responses input items (R1.6).

Image refs: a stored ``user`` / ``system_event`` item may hold ``cgbot_image`` parts (see
``cgbot.vision``). Rendering swaps them for ``input_image`` parts when the ref has a loaded data
URL (the newest image-bearing turns, ``recent_image_refs``) and drops them otherwise.

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
from collections.abc import Mapping, Sequence
from typing import Any, cast

from cgbot.provider import function_call_output, user_message
from cgbot.session import Item, Kind
from cgbot.vision.intake import IMAGE_REF_PART, item_image_refs

COMPACTION_PREFIX = "以下是此前对话的压缩摘要,供延续上下文:\n"
CANCELLED_OUTPUT = (
    "cancelled: the turn was stopped before this tool returned; no result was produced."
)
INTERRUPTED_OUTPUT = (
    "interrupted: the previous run ended before this tool returned; no result was produced."
)

_ITEM_KINDS = frozenset({"user", "system_event", "assistant", "tool_call"})
_IMAGE_KINDS = frozenset({"user", "system_event"})


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


def to_input_items(
    items: Sequence[Item], images: Mapping[str, str] | None = None
) -> list[dict[str, Any]]:
    """Convert an active window (``load_active`` order) into Responses input items.

    ``images`` maps image refs to data URLs (see ``recent_image_refs``).
    """
    return render_items(expand_window(items), images)


def recent_image_refs(expanded: Sequence[Item], turns: int) -> list[str]:
    """Refs of the images in the last ``turns`` image-bearing user turns of a window.

    Only these are worth loading and sending; every older image degrades to the text marker the
    turn already carries.
    """
    if turns <= 0:
        return []
    per_turn = [
        refs
        for item in expanded
        if item.kind in _IMAGE_KINDS and "item" in item.payload
        if (refs := item_image_refs(item.payload["item"]))
    ]
    return [ref for refs in per_turn[-turns:] for ref in refs]


def _with_images(item: dict[str, Any], images: Mapping[str, str]) -> dict[str, Any]:
    """Replace ``cgbot_image`` parts by ``input_image`` parts; unresolved ones are dropped."""
    content = item.get("content")
    if not isinstance(content, list):
        return item
    parts: list[Any] = []
    for part in content:
        if isinstance(part, dict) and part.get("type") == IMAGE_REF_PART:
            url = images.get(str(part.get("ref")))
            if url is not None:
                parts.append({"type": "input_image", "image_url": url})
        else:
            parts.append(part)
    return {**item, "content": parts}


def render_items(
    expanded: Sequence[Item], images: Mapping[str, str] | None = None
) -> list[dict[str, Any]]:
    """Render an already expanded window (see ``expand_window``) into input items.

    Stored ``cgbot_image`` parts become ``input_image`` parts for refs present in ``images`` and
    are dropped otherwise, so no ref ever leaves the process.
    """
    resolved = images or {}
    called = {str(i.payload["call_id"]) for i in expanded if i.kind == "tool_call"}
    answered: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in expanded:
        payload = item.payload
        if item.kind == "compaction":
            out.append(user_message(COMPACTION_PREFIX + str(payload["summary"])))
        elif item.kind in _ITEM_KINDS:
            if "item" in payload:
                rendered = copy.deepcopy(dict(payload["item"]))
                out.append(
                    _with_images(rendered, resolved) if item.kind in _IMAGE_KINDS else rendered
                )
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
