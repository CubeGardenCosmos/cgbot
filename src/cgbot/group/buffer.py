"""Buffered unhandled group messages, kept as proactive-opener context (R3.3)."""

from __future__ import annotations

from dataclasses import dataclass

from cgbot.group.text import plain_text
from cgbot.model import InboundMessage

DEFAULT_BUFFER_CAPACITY = 30


@dataclass(frozen=True, slots=True)
class BufferedMessage:
    message_id: str
    user_id: str
    ts: float
    text: str


def buffered_from(msg: InboundMessage) -> BufferedMessage:
    return BufferedMessage(
        message_id=msg.message_id,
        user_id=msg.user_id,
        ts=msg.time,
        text=plain_text(msg.segments),
    )
