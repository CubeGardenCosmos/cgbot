"""Tool contract for the agent loop (R1.6): specs, per-call context and the registry.

Identity is never a tool argument: ``ToolContext.user_id`` and ``source_channel`` come from the
event that started the turn. Tools that choose a channel do so from ``ToolContext.channels``.
"""

from __future__ import annotations

import copy
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from cgbot.model import Channel, OutboundMessage, SendResult


@dataclass(frozen=True, slots=True)
class ToolResult:
    """What the model sees for one call. ``is_error`` marks failed or refused calls."""

    output: str
    is_error: bool = False


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Per-call context. ``channels`` lists the user's reachable channels; ``source_channel``
    is always included and first. ``sender`` is the runner's outbound sender."""

    user_id: str
    session_id: str
    source_channel: Channel
    source_message_id: str | None
    sender: Callable[[OutboundMessage], Awaitable[SendResult]]
    channels: tuple[Channel, ...] = ()


ToolHandler = Callable[[ToolContext, dict[str, Any]], Awaitable[ToolResult]]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """A model-callable tool. ``channel_param`` names a property of ``parameters_schema`` whose
    enum is filled per turn with the user's channel keys (``qq:<account>``)."""

    name: str
    description: str
    parameters_schema: dict[str, Any]
    handler: ToolHandler
    channel_param: str | None = None


def channel_key(channel: Channel) -> str:
    """Stable textual form of a channel shown to the model, e.g. ``qq:10001``."""
    return f"{channel.kind}:{channel.account}"


def available_channels(source: Channel, channels: Iterable[Channel]) -> tuple[Channel, ...]:
    """Source channel first, then the other given channels without duplicates."""
    out: list[Channel] = [source]
    for channel in channels:
        if channel not in out:
            out.append(channel)
    return tuple(out)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if not spec.name:
            raise ValueError("tool name must be non-empty")
        if spec.name in self._tools:
            raise ValueError(f"tool already registered: {spec.name!r}")
        if spec.channel_param is not None and spec.channel_param not in (
            spec.parameters_schema.get("properties") or {}
        ):
            raise ValueError(f"channel_param {spec.channel_param!r} is not a schema property")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def schemas(self, channels: Sequence[Channel] = ()) -> list[dict[str, Any]]:
        """Responses ``function`` tool definitions for one request."""
        out: list[dict[str, Any]] = []
        for spec in self._tools.values():
            params = copy.deepcopy(spec.parameters_schema)
            if spec.channel_param is not None and channels:
                params["properties"][spec.channel_param]["enum"] = [
                    channel_key(c) for c in channels
                ]
            out.append(
                {
                    "type": "function",
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": params,
                    "strict": False,
                }
            )
        return out
