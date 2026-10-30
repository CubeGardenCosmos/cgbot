"""Shared platform-neutral types. Owned by lead; change only with lead approval."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

ChannelKind = Literal["qq"]  # extend later (weixin); keep as the single source


@dataclass(frozen=True, slots=True)
class Channel:
    """A reachable endpoint of a user (入口). Private QQ chat in M1."""
    kind: ChannelKind
    account: str  # QQ number as str


@dataclass(frozen=True, slots=True)
class Segment:
    """One message segment. type in {text,image,at,reply,record,video,face,file,forward,other}."""
    type: str
    data: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class InboundMessage:
    message_id: str
    scope: Literal["private", "group"]
    user_id: str          # sender QQ
    group_id: str | None  # None for private
    segments: tuple[Segment, ...]
    time: float           # unix seconds
    reply_to: str | None = None  # quoted message_id


@dataclass(frozen=True, slots=True)
class InputStatus:
    """NapCat notify/input_status (private only)."""
    user_id: str
    event_type: int
    status_text: str
    time: float


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    scope: Literal["private", "group"]
    target_id: str  # user_id or group_id
    segments: tuple[Segment, ...]
    reply_to: str | None = None


@dataclass(frozen=True, slots=True)
class SendResult:
    ok: bool
    message_id: str | None = None
    error: str | None = None
