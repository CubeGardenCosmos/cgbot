"""Daily push of the 世界信号 digest to subscribers (asyncio poll loop).

Each poll reads today's delivery. Nothing happens before ``push_time`` (local time). After it, an
issue is pushed once per subscriber: at once when complete, otherwise only once ``give_up_hour``
is reached, when whatever tiers exist are sent (or an apology when none do). Every attempt is
recorded in the store, so restarts never resend and a failure is logged, not retried every poll.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, time, tzinfo
from pathlib import Path
from typing import Final

from cgbot.model import OutboundMessage, Segment, SendResult
from cgbot.news.delivery import (
    PARTIAL_NOTE,
    TIER_NAMES,
    Delivery,
    build_issue_messages,
    read_delivery,
)
from cgbot.news.store import NewsStore

log = logging.getLogger(__name__)

Sender = Callable[[OutboundMessage], Awaitable[SendResult]]
TargetOf = Callable[[str], Awaitable[str]]
Clock = Callable[[], datetime]

POLL_SECONDS: Final = 30.0
GAP_SECONDS: Final = 1.0  # pause between messages to one subscriber


def local_clock(tz: tzinfo) -> Clock:
    return lambda: datetime.now(tz)


def apology_text(issue_id: str) -> str:
    return f"📰 世界信号 {issue_id}:今天的刊没能按时出来,生成端会继续补做,稍后可以再让我取一次。"


async def send_groups(
    send: Sender,
    account: str,
    groups: Sequence[tuple[Segment, ...]],
    *,
    gap_seconds: float,
) -> bool:
    """Send each group as one private message, in order. False when any send was refused."""
    ok = True
    for index, segments in enumerate(groups):
        if index:
            await asyncio.sleep(gap_seconds)
        result = await send(OutboundMessage(scope="private", target_id=account, segments=segments))
        if not result.ok:
            ok = False
            log.error("news send refused: index=%d error=%s", index, result.error)
    return ok


def compose_push(delivery: Delivery | None, issue_id: str) -> tuple[tuple[Segment, ...], ...]:
    """Messages for one scheduled push: all tiers, partial/unavailable notes, or an apology."""
    if delivery is None:
        return ((Segment("text", {"text": apology_text(issue_id)}),),)
    built = build_issue_messages(delivery, TIER_NAMES)
    if not built.groups:
        return ((Segment("text", {"text": apology_text(issue_id)}),),)
    groups = list(built.groups)
    tail: list[str] = []
    if not delivery.complete:
        tail.append(PARTIAL_NOTE)
    if built.notes:
        tail.append("未附上:" + ";".join(built.notes))
    if tail:
        groups.append((Segment("text", {"text": "\n".join(tail)}),))
    return tuple(groups)


class NewsPusher:
    """Poll loop that pushes the digest once per subscriber per issue."""

    def __init__(
        self,
        *,
        store: NewsStore,
        output_dir: Path,
        send: Sender,
        target_of: TargetOf,
        push_time: time,
        give_up_hour: int,
        clock: Clock,
        poll_seconds: float = POLL_SECONDS,
        gap_seconds: float = GAP_SECONDS,
    ) -> None:
        self._store = store
        self._output_dir = output_dir
        self._send = send
        self._target_of = target_of
        self._push_time = push_time
        self._give_up_hour = give_up_hour
        self._clock = clock
        self._poll_seconds = poll_seconds
        self._gap_seconds = gap_seconds

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("news push poll failed")
            await asyncio.sleep(self._poll_seconds)

    async def tick(self) -> int:
        """One poll. Returns how many subscribers were attempted."""
        now = self._clock()
        if now.time() < self._push_time:
            return 0
        day = now.date().isoformat()
        delivery = await asyncio.to_thread(read_delivery, self._output_dir, day)
        complete = delivery is not None and delivery.complete
        if not complete and now.hour < self._give_up_hour:
            return 0  # the producer is still writing today's issue
        issue_id = delivery.issue_id if delivery is not None else day
        pending = [
            user_id
            for user_id in await self._store.subscribers()
            if not await self._store.was_sent(user_id, issue_id)
        ]
        if not pending:
            return 0
        groups = compose_push(delivery, issue_id)
        for user_id in pending:
            await self._push_one(user_id, issue_id, groups)
        return len(pending)

    async def _push_one(
        self, user_id: str, issue_id: str, groups: Sequence[tuple[Segment, ...]]
    ) -> None:
        try:
            account = await self._target_of(user_id)
            ok = await send_groups(self._send, account, groups, gap_seconds=self._gap_seconds)
            log.info("news pushed: issue=%s user=%s ok=%s", issue_id, user_id, ok)
        except Exception:
            log.exception("news push failed: issue=%s user=%s", issue_id, user_id)
        try:
            await self._store.mark_sent(user_id, issue_id)
        except Exception:
            log.exception("news ledger write failed: issue=%s user=%s", issue_id, user_id)
