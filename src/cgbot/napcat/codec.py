"""OneBot11 (NapCat, array format) <-> internal model conversion. Pure functions, no I/O.

Scope filtering (friends / fixed groups) is NOT done here; group messages are parsed with
``scope="group"`` and the allowlist owner (R1.1) drops them in wiring.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Literal

from cgbot.model import InboundMessage, InputStatus, OutboundMessage, Segment

# Segment types the internal model names explicitly; anything else becomes "other".
INBOUND_SEGMENT_TYPES = frozenset(
    {"text", "image", "at", "reply", "record", "video", "face", "file", "forward"}
)
# Outbound M1 set. "forward" needs node construction and "other" is unknown: both rejected.
OUTBOUND_SEGMENT_TYPES = frozenset(
    {"text", "image", "at", "reply", "record", "video", "face", "file"}
)


def _parse_segment(raw: object) -> Segment | None:
    if not isinstance(raw, Mapping):
        return None
    seg_type = raw.get("type")
    raw_data = raw.get("data")
    data: dict[str, object] = dict(raw_data) if isinstance(raw_data, Mapping) else {}
    if not isinstance(seg_type, str) or seg_type not in INBOUND_SEGMENT_TYPES:
        return Segment("other", {"onebot_type": seg_type, **data})
    # IDs are normalised to str so they compare equal across int/str wire forms.
    if seg_type == "at" and "qq" in data:
        data["qq"] = str(data["qq"])
    elif seg_type in ("reply", "forward") and "id" in data:
        data["id"] = str(data["id"])
    return Segment(seg_type, data)


def _event_time(raw: Mapping[str, object]) -> float:
    value = raw.get("time")
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return time.time()  # NapCat always sends time; fall back to receive time


def _parse_message(raw: Mapping[str, object], self_id: str) -> InboundMessage | None:
    if raw.get("user_id") is None or raw.get("message_id") is None:
        return None
    user_id = str(raw["user_id"])
    if user_id == self_id:
        return None  # the bot's own messages echoed back by NapCat
    message_type = raw.get("message_type")
    scope: Literal["private", "group"]
    group_id: str | None
    is_friend = False
    if message_type == "private":
        scope, group_id = "private", None
        is_friend = raw.get("sub_type") == "friend"  # temp sessions from group members: "group"
    elif message_type == "group" and raw.get("group_id") is not None:
        scope, group_id = "group", str(raw["group_id"])
    else:
        return None
    items = raw.get("message")
    if not isinstance(items, list):
        return None  # string (CQ-code) messages: M1 requires messagePostFormat=array
    segments = tuple(seg for seg in map(_parse_segment, items) if seg is not None)
    reply_to = next(
        (str(seg.data["id"]) for seg in segments if seg.type == "reply" and "id" in seg.data),
        None,
    )
    return InboundMessage(
        message_id=str(raw["message_id"]),
        scope=scope,
        user_id=user_id,
        group_id=group_id,
        segments=segments,
        time=_event_time(raw),
        reply_to=reply_to,
        is_friend=is_friend,
    )


def _parse_input_status(raw: Mapping[str, object]) -> InputStatus | None:
    if raw.get("sub_type") != "input_status":
        return None
    user_id = raw.get("user_id")
    event_type = raw.get("event_type")
    if user_id is None or not isinstance(event_type, int) or isinstance(event_type, bool):
        return None
    if raw.get("group_id", 0) not in (0, "0", None):
        return None  # input status is private-only
    status_text = raw.get("status_text", "")
    return InputStatus(
        user_id=str(user_id),
        event_type=event_type,
        # Kept verbatim; an empty text is NOT interpreted as "stopped typing".
        status_text=status_text if isinstance(status_text, str) else "",
        time=_event_time(raw),
    )


def parse_event(raw: Mapping[str, object]) -> InboundMessage | InputStatus | None:
    """Convert one reverse-WS event (already JSON-decoded) into an internal message or
    input status.

    Returns None for meta_event/heartbeat, unknown notices, messages sent by the bot itself
    (``user_id == self_id``), missing/malformed fields. Uses ``raw["self_id"]`` to drop self
    messages; an event without it is treated as malformed.
    """
    post_type = raw.get("post_type")
    if post_type == "message":
        self_id = raw.get("self_id")
        if self_id is None:
            return None
        return _parse_message(raw, str(self_id))
    if post_type == "notice" and raw.get("notice_type") == "notify":
        return _parse_input_status(raw)
    return None


def _segment_to_onebot(seg: Segment) -> dict[str, object]:
    if seg.type not in OUTBOUND_SEGMENT_TYPES:
        raise ValueError(f"unsupported outbound segment type: {seg.type}")
    return {"type": seg.type, "data": dict(seg.data)}


def build_send(message: OutboundMessage) -> tuple[str, dict[str, object]]:
    """Internal outbound message -> (OneBot action, params), array segments.

    ``reply_to`` becomes a leading reply segment. Raises ValueError when the message cannot
    be expressed: unsupported segment type, non-numeric target, unknown scope, or both
    ``reply_to`` and an inline reply segment.
    """
    if not message.target_id.isdigit():
        raise ValueError("target_id must be a numeric QQ id")
    has_inline_reply = any(seg.type == "reply" for seg in message.segments)
    if message.reply_to is not None and has_inline_reply:
        raise ValueError("reply_to and an inline reply segment are both set")
    segments: list[dict[str, object]] = []
    if message.reply_to is not None:
        segments.append({"type": "reply", "data": {"id": message.reply_to}})
    segments.extend(_segment_to_onebot(seg) for seg in message.segments)
    target = int(message.target_id)
    if message.scope == "private":
        return "send_private_msg", {"user_id": target, "message": segments}
    if message.scope == "group":
        return "send_group_msg", {"group_id": target, "message": segments}
    raise ValueError(f"unknown scope: {message.scope}")
