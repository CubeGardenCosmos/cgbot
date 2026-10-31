"""Private-chat dispatcher (R2.1 typing debounce, R2.2 steering / interrupt).

One lane per private session (``private:<user_id>``). A lane runs as one background task that
owns the decision of when the next turn starts:

- Quiet window (R2.1): no model request while the user is typing. Every input_status push and
  every inbound message is a "last event"; the next turn starts ``QUIET_SECONDS`` after the
  latest one. There is no maximum wait.
- Steering (R2.2): a message that arrives while a turn runs is injected into it if the turn has
  already called a tool (it reaches the model with the next request). Otherwise the running turn
  is interrupted and the message waits for the quiet window; the restarted turn carries only the
  messages not yet passed to any turn, so no user message is stored twice.

Background wakes (``on_wake``) are system-originated and never interrupt or inject. They join the
lane queue and run after the running turn and the quiet window, like any other message.

Messages are never reordered: injection only happens when nothing is waiting in the lane.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from cgbot.agent import AgentRunner, TurnContext, TurnResult
from cgbot.dispatch.timebase import SystemTimebase, Timebase
from cgbot.model import Channel

log = logging.getLogger(__name__)

QUIET_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class PrivateMessage:
    """One inbound private message, already resolved to its session.

    ``item`` is the Responses input item for the message text (``user_message``). ``None`` starts
    a turn that adds no input item: its input is already stored (a Coding Agent notice record).
    """

    session_id: str
    user_id: str
    message_id: str | None  # None for background wakes
    item: dict[str, Any] | None  # None: a turn over stored history only (a notice record)
    channel: Channel
    channels: tuple[Channel, ...]


@dataclass(eq=False)
class _Lane:
    session_id: str
    pending: list[PrivateMessage] = field(default_factory=list)
    last_event: float = 0.0
    task: asyncio.Task[None] | None = None


class PrivateDispatcher:
    """Decides when private turns start and how new messages reach the running turn."""

    def __init__(
        self,
        runner: AgentRunner,
        timebase: Timebase | None = None,
        *,
        on_turn_done: Callable[[str, TurnResult], Awaitable[None]] | None = None,
    ) -> None:
        self._runner = runner
        self._time: Timebase = timebase if timebase is not None else SystemTimebase()
        self._lanes: dict[str, _Lane] = {}
        self._closing = False
        self._on_turn_done = on_turn_done

    def on_typing(self, session_id: str) -> None:
        """An input_status push for this session. Any push counts, including empty status text.

        Only a lane that already holds work (waiting or running) is affected: a session with no
        pending message has nothing to delay.
        """
        lane = self._lanes.get(session_id)
        if lane is not None:
            lane.last_event = self._time.now()

    def on_message(self, message: PrivateMessage) -> None:
        if self._closing:
            log.info("private message dropped during shutdown: message_id=%s", message.message_id)
            return
        sid = message.session_id
        lane = self._lane_for(sid)
        lane.last_event = self._time.now()
        if self._runner.is_running(sid):
            if (
                not lane.pending
                and self._runner.tool_called(sid)
                and message.item is not None
                and self._runner.inject(sid, [message.item], message_id=message.message_id)
            ):
                log.info("message injected into running turn: session=%s", sid)
                return
            lane.pending.append(message)
            # Interrupt. False when the turn is already stopping; the message then waits.
            if self._runner.cancel(sid):
                log.info("running turn interrupted for new message: session=%s", sid)
            return
        lane.pending.append(message)

    def on_wake(self, message: PrivateMessage) -> bool:
        """System-originated input (background wake). Never interrupts or injects.

        The wake joins the lane queue. A running turn is left alone: the wake runs after that turn
        and the quiet window. ``item=None`` queues a turn over stored history only (a notice
        record is already the input). Returns False when dropped during shutdown.
        """
        if self._closing:
            log.info("private wake dropped during shutdown: session=%s", message.session_id)
            return False
        lane = self._lane_for(message.session_id)
        lane.pending.append(message)
        lane.last_event = self._time.now()
        return True

    def _lane_for(self, sid: str) -> _Lane:
        lane = self._lanes.get(sid)
        if lane is None:
            lane = _Lane(session_id=sid)
            self._lanes[sid] = lane
            lane.task = asyncio.create_task(self._drive(lane), name=f"private:{sid}")
        return lane

    async def close(self) -> None:
        """Cancel every lane (and the turn it runs). Messages not yet started are dropped."""
        self._closing = True
        lanes = list(self._lanes.values())
        dropped = sum(len(lane.pending) for lane in lanes)
        if dropped:
            log.warning("private messages dropped at shutdown: count=%d", dropped)
        tasks = [lane.task for lane in lanes if lane.task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._lanes.clear()

    async def _drive(self, lane: _Lane) -> None:
        try:
            while lane.pending:
                await self._wait_quiet(lane)
                # No await between taking the batch and the runner registering the turn, so
                # anything that arrives from now on sees the turn as running.
                batch, lane.pending = lane.pending, []
                await self._run(lane, batch)
        finally:
            if self._lanes.get(lane.session_id) is lane:
                del self._lanes[lane.session_id]

    async def _wait_quiet(self, lane: _Lane) -> None:
        while (remaining := lane.last_event + QUIET_SECONDS - self._time.now()) > 0:
            await self._time.sleep(remaining)

    async def _run(self, lane: _Lane, batch: list[PrivateMessage]) -> None:
        last = batch[-1]
        items = [m.item for m in batch if m.item is not None]
        ctx = TurnContext(
            user_id=last.user_id,
            session_id=lane.session_id,
            source_channel=last.channel,
            source_message_id=last.message_id,
            new_items=items,
            reason="user" if items else "system_event",
            channels=last.channels,
            new_message_ids=tuple(m.message_id for m in batch if m.item is not None),
        )
        try:
            result = await self._runner.run_turn(ctx)
        except Exception as exc:
            log.error("turn crashed: session=%s error=%s", lane.session_id, type(exc).__name__)
            return
        log.info(
            "turn finished: session=%s status=%s tool_called=%s replies=%d",
            lane.session_id,
            result.status,
            result.tool_called,
            result.replies_sent,
        )
        if self._on_turn_done is not None:
            try:
                await self._on_turn_done(lane.session_id, result)
            except Exception as exc:
                log.error(
                    "turn-done hook failed: session=%s error=%s",
                    lane.session_id,
                    type(exc).__name__,
                )
