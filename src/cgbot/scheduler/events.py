"""System event value type and the in-memory event queue (R1.7).

A system event wakes the agent without a user message. It is attributed to a user (or to no
user for global events) and never carries a fabricated platform identity. User messages do
not go through this path.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

# Kinds known to this module. Kinds are free-form non-empty strings; consumers match on them.
KIND_BACKGROUND_NOTICE = "background_notice"  # background work finished or failed (M5 reports etc.)
KIND_COMPACTION_IDLE = "compaction_idle"  # 8h idle compaction trigger (R2.3 registers it)


@dataclass(frozen=True, slots=True)
class SystemEvent:
    event_id: str
    kind: str
    user_id: str | None
    payload: dict[str, Any]
    due_at: float


class SystemEventQueue:
    """Unbounded in-memory FIFO for events produced by other subsystems.

    Not persisted: events queued here are lost on process exit. Use ``Scheduler`` for anything
    that must survive restart. ``put`` never blocks (the queue is unbounded).
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[SystemEvent] = asyncio.Queue()

    def put(self, event: SystemEvent) -> None:
        if not isinstance(event, SystemEvent):
            raise TypeError("SystemEventQueue accepts SystemEvent only")
        self._queue.put_nowait(event)

    async def get(self) -> SystemEvent:
        return await self._queue.get()

    def qsize(self) -> int:
        return self._queue.qsize()
