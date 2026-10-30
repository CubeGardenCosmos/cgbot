from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

from cgbot.scheduler import (
    KIND_BACKGROUND_NOTICE,
    KIND_COMPACTION_IDLE,
    Scheduler,
    SchedulerError,
    SystemEvent,
    SystemEventQueue,
)
from cgbot.session import SessionStore

# Safety net for awaits on the loop; never used as a sleep.
TIMEOUT = 5.0
EIGHT_HOURS = 8 * 3600.0


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeSleep:
    """Stands in for asyncio.sleep: each call parks until the test resolves its future."""

    def __init__(self) -> None:
        self.calls: asyncio.Queue[tuple[float, asyncio.Future[None]]] = asyncio.Queue()

    async def __call__(self, delay: float) -> None:
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.calls.put_nowait((delay, fut))
        await fut

    async def next_call(self) -> tuple[float, asyncio.Future[None]]:
        return await asyncio.wait_for(self.calls.get(), TIMEOUT)


class Deliveries:
    """Handler that records deliveries, fails a set number of times, or blocks on a gate."""

    def __init__(self) -> None:
        self.events: list[SystemEvent] = []
        self.signal: asyncio.Queue[SystemEvent] = asyncio.Queue()
        self.started = asyncio.Event()
        self.failures = 0
        self.attempts = 0
        self.in_flight = 0
        self.max_in_flight = 0
        self.gate: asyncio.Event | None = None

    async def __call__(self, event: SystemEvent) -> None:
        self.attempts += 1
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        self.started.set()
        try:
            if self.gate is not None:
                await self.gate.wait()
            if self.failures > 0:
                self.failures -= 1
                raise RuntimeError("handler boom")
            self.events.append(event)
            self.signal.put_nowait(event)
        finally:
            self.in_flight -= 1

    async def next_delivery(self) -> SystemEvent:
        return await asyncio.wait_for(self.signal.get(), TIMEOUT)


async def settle() -> None:
    """Yield to the loop a few times so queued work runs; no wall-clock wait."""
    for _ in range(10):
        await asyncio.sleep(0)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "cgbot.sqlite"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def sleep() -> FakeSleep:
    return FakeSleep()


@pytest.fixture
def deliveries() -> Deliveries:
    return Deliveries()


@pytest.fixture
async def make() -> AsyncIterator[Callable[..., Scheduler]]:
    """Builds schedulers and tears them down (stop, then close) after each test."""
    created: list[Scheduler] = []

    def factory(
        db: Path, handler: Any, clock: FakeClock, sleep: FakeSleep, **kwargs: Any
    ) -> Scheduler:
        s = Scheduler(db, handler, clock=clock, sleep=sleep, **kwargs)
        created.append(s)
        return s

    yield factory
    for s in created:
        await s.stop()
        s.close()


async def test_no_delivery_before_due_then_fires_at_due_time(
    db_path: Path,
    clock: FakeClock,
    sleep: FakeSleep,
    deliveries: Deliveries,
    make: Callable[..., Scheduler],
) -> None:
    s = make(db_path, deliveries, clock, sleep)
    eid = s.schedule(KIND_BACKGROUND_NOTICE, "u1", {"text": "done"}, due_at=1100.0)
    await s.start()

    delay, fut = await sleep.next_call()
    assert delay == pytest.approx(100.0)
    assert deliveries.attempts == 0

    # Woken before due (spurious): must sleep the remainder, not deliver.
    clock.now = 1099.5
    fut.set_result(None)
    delay, fut = await sleep.next_call()
    assert delay == pytest.approx(0.5)
    assert deliveries.attempts == 0

    clock.now = 1100.0
    fut.set_result(None)
    event = await deliveries.next_delivery()
    assert event == SystemEvent(
        event_id=eid,
        kind=KIND_BACKGROUND_NOTICE,
        user_id="u1",
        payload={"text": "done"},
        due_at=1100.0,
    )
    assert s.pending() == []


async def test_orders_by_due_time_then_insertion_across_users(
    db_path: Path,
    clock: FakeClock,
    sleep: FakeSleep,
    deliveries: Deliveries,
    make: Callable[..., Scheduler],
) -> None:
    s = make(db_path, deliveries, clock, sleep)
    a = s.schedule("k", "u1", {"n": 1}, due_at=990.0)
    b = s.schedule("k", "u2", {"n": 2}, due_at=1000.0)
    c = s.schedule("k", "u1", {"n": 3}, due_at=990.0)
    d = s.schedule("k", "u2", {"n": 4}, due_at=995.0)
    assert [e.event_id for e in s.pending()] == [a, c, d, b]

    await s.start()
    got = [(await deliveries.next_delivery()).event_id for _ in range(4)]
    assert got == [a, c, d, b]
    assert deliveries.max_in_flight == 1  # serial: no parallel handler calls
    assert s.pending() == []


async def test_earlier_event_scheduled_while_sleeping_wakes_loop_early(
    db_path: Path,
    clock: FakeClock,
    sleep: FakeSleep,
    deliveries: Deliveries,
    make: Callable[..., Scheduler],
) -> None:
    s = make(db_path, deliveries, clock, sleep)
    late = s.schedule("k", "u1", {}, due_at=2000.0)
    await s.start()
    delay, old_fut = await sleep.next_call()
    assert delay == pytest.approx(1000.0)

    early = s.schedule("k", "u2", {}, due_at=1500.0)
    delay, fut = await sleep.next_call()
    assert delay == pytest.approx(500.0)
    assert old_fut.cancelled()

    clock.now = 1500.0
    fut.set_result(None)
    assert (await deliveries.next_delivery()).event_id == early

    delay, _ = await sleep.next_call()
    assert delay == pytest.approx(500.0)
    assert [e.event_id for e in s.pending()] == [late]


async def test_cancel_pending_head_reschedules_sleep_to_next(
    db_path: Path,
    clock: FakeClock,
    sleep: FakeSleep,
    deliveries: Deliveries,
    make: Callable[..., Scheduler],
) -> None:
    s = make(db_path, deliveries, clock, sleep)
    first = s.schedule("k", "u1", {}, due_at=1100.0)
    second = s.schedule("k", "u1", {}, due_at=1200.0)
    await s.start()
    delay, _ = await sleep.next_call()
    assert delay == pytest.approx(100.0)

    assert s.cancel(first) is True
    assert s.cancel(first) is False  # already cancelled
    assert s.cancel("no-such-event") is False
    assert [e.event_id for e in s.pending()] == [second]

    delay, fut = await sleep.next_call()
    assert delay == pytest.approx(200.0)
    clock.now = 1200.0
    fut.set_result(None)
    assert (await deliveries.next_delivery()).event_id == second
    assert deliveries.attempts == 1


async def test_compaction_idle_trigger_fires_once_after_eight_hours_without_user_message(
    db_path: Path,
    clock: FakeClock,
    sleep: FakeSleep,
    deliveries: Deliveries,
    make: Callable[..., Scheduler],
) -> None:
    s = make(db_path, deliveries, clock, sleep)
    eid = s.schedule(
        KIND_COMPACTION_IDLE, "u1", {"session_id": "private:u1"}, clock.now + EIGHT_HOURS
    )
    await s.start()
    delay, fut = await sleep.next_call()
    assert delay == pytest.approx(EIGHT_HOURS)

    # Activity at +4h moves the idle deadline (R2.3 owns the idle definition).
    clock.now += 4 * 3600.0
    assert s.reschedule(eid, clock.now + EIGHT_HOURS) is True
    fut.set_result(None)  # wakes early; loop recomputes from the new deadline
    delay, fut = await sleep.next_call()
    assert delay == pytest.approx(EIGHT_HOURS)

    clock.now += EIGHT_HOURS
    fut.set_result(None)
    event = await deliveries.next_delivery()
    assert event.event_id == eid
    assert event.kind == KIND_COMPACTION_IDLE
    assert event.payload == {"session_id": "private:u1"}

    await settle()
    assert sleep.calls.empty()  # nothing left to wait for
    assert deliveries.attempts == 1
    assert s.reschedule(eid, clock.now + 1.0) is False  # already delivered


async def test_restart_recovers_pending_and_never_redelivers_delivered(
    db_path: Path,
    clock: FakeClock,
    sleep: FakeSleep,
    make: Callable[..., Scheduler],
) -> None:
    d1 = Deliveries()
    s1 = make(db_path, d1, clock, sleep)
    done = s1.schedule("k", "u1", {"i": 1}, due_at=900.0)
    later = s1.schedule("k", "u2", {"i": 2}, due_at=1500.0)
    await s1.start()
    assert (await d1.next_delivery()).event_id == done
    delay, _ = await sleep.next_call()
    assert delay == pytest.approx(500.0)
    await s1.stop()
    s1.close()

    d2 = Deliveries()
    s2 = make(db_path, d2, clock, sleep)
    assert [e.event_id for e in s2.pending()] == [later]
    await s2.start()
    delay, fut = await sleep.next_call()
    assert delay == pytest.approx(500.0)
    clock.now = 1500.0
    fut.set_result(None)
    assert (await d2.next_delivery()).event_id == later
    await s2.stop()
    s2.close()

    d3 = Deliveries()
    s3 = make(db_path, d3, clock, sleep)
    assert s3.pending() == []
    await s3.start()
    await settle()
    assert sleep.calls.empty()
    assert d3.attempts == 0


async def test_events_scheduled_before_restart_and_overdue_after_restart_are_delivered(
    db_path: Path,
    clock: FakeClock,
    sleep: FakeSleep,
    make: Callable[..., Scheduler],
) -> None:
    s1 = make(db_path, Deliveries(), clock, sleep)
    eid = s1.schedule("k", "u1", {"x": True}, due_at=1100.0)
    s1.close()  # process ends before the loop ever ran

    clock.now = 1200.0  # restarted after the due time: late delivery, not discarded
    d2 = Deliveries()
    s2 = make(db_path, d2, clock, sleep)
    await s2.start()
    assert (await d2.next_delivery()).event_id == eid
    assert sleep.calls.empty()  # overdue: no waiting at all


async def test_handler_failure_keeps_event_pending_and_retries_with_bounded_backoff(
    db_path: Path,
    clock: FakeClock,
    sleep: FakeSleep,
    deliveries: Deliveries,
    make: Callable[..., Scheduler],
) -> None:
    s = make(db_path, deliveries, clock, sleep, retry_base_delay=10.0, retry_max_delay=25.0)
    deliveries.failures = 4
    eid = s.schedule("k", "u1", {}, due_at=clock.now)
    await s.start()

    for expected in (10.0, 20.0, 25.0, 25.0):
        delay, fut = await sleep.next_call()
        assert delay == pytest.approx(expected)
        assert [e.event_id for e in s.pending()] == [eid]  # still pending during backoff
        clock.now += delay
        fut.set_result(None)

    assert (await deliveries.next_delivery()).event_id == eid
    assert deliveries.attempts == 5
    assert s.pending() == []


async def test_failing_head_blocks_later_events_until_it_succeeds(
    db_path: Path,
    clock: FakeClock,
    sleep: FakeSleep,
    deliveries: Deliveries,
    make: Callable[..., Scheduler],
) -> None:
    s = make(db_path, deliveries, clock, sleep, retry_base_delay=10.0)
    deliveries.failures = 1
    head = s.schedule("k", "u1", {"n": 1}, due_at=clock.now)
    tail = s.schedule("k", "u2", {"n": 2}, due_at=clock.now)
    await s.start()

    delay, fut = await sleep.next_call()
    assert delay == pytest.approx(10.0)
    assert deliveries.events == []  # tail must not overtake the failed head
    clock.now += delay
    fut.set_result(None)
    assert (await deliveries.next_delivery()).event_id == head
    assert (await deliveries.next_delivery()).event_id == tail


async def test_stop_is_clean_while_sleeping_and_idempotent(
    db_path: Path,
    clock: FakeClock,
    sleep: FakeSleep,
    deliveries: Deliveries,
    make: Callable[..., Scheduler],
) -> None:
    s = make(db_path, deliveries, clock, sleep)
    s.schedule("k", "u1", {}, due_at=5000.0)
    await s.start()
    _, fut = await sleep.next_call()

    await s.stop()
    assert fut.cancelled()
    assert "cgbot-scheduler" not in {t.get_name() for t in asyncio.all_tasks()}
    await s.stop()  # second stop is a no-op
    assert deliveries.attempts == 0
    assert len(s.pending()) == 1


async def test_stop_during_handler_cancels_delivery_and_event_is_redelivered(
    db_path: Path,
    clock: FakeClock,
    sleep: FakeSleep,
    make: Callable[..., Scheduler],
) -> None:
    d1 = Deliveries()
    d1.gate = asyncio.Event()  # never released: handler stays in flight
    s1 = make(db_path, d1, clock, sleep)
    eid = s1.schedule("k", "u1", {"msg": "x"}, due_at=clock.now)
    await s1.start()
    await asyncio.wait_for(d1.started.wait(), TIMEOUT)

    assert s1.cancel(eid) is False  # running deliveries cannot be withdrawn
    assert s1.reschedule(eid, clock.now + 1.0) is False
    await s1.stop()
    assert [e.event_id for e in s1.pending()] == [eid]
    s1.close()

    d2 = Deliveries()
    s2 = make(db_path, d2, clock, sleep)
    await s2.start()
    assert (await d2.next_delivery()).event_id == eid
    assert d2.attempts == 1


async def test_start_twice_is_rejected(
    db_path: Path,
    clock: FakeClock,
    sleep: FakeSleep,
    deliveries: Deliveries,
    make: Callable[..., Scheduler],
) -> None:
    s = make(db_path, deliveries, clock, sleep)
    await s.start()
    with pytest.raises(SchedulerError):
        await s.start()
    await s.stop()
    s.close()


async def test_close_while_running_is_rejected(
    db_path: Path,
    clock: FakeClock,
    sleep: FakeSleep,
    deliveries: Deliveries,
    make: Callable[..., Scheduler],
) -> None:
    s = make(db_path, deliveries, clock, sleep)
    await s.start()
    with pytest.raises(SchedulerError):
        s.close()
    await s.stop()
    s.close()  # now allowed; teardown close is a no-op


async def test_invalid_arguments_are_rejected(
    db_path: Path,
    clock: FakeClock,
    sleep: FakeSleep,
    deliveries: Deliveries,
    make: Callable[..., Scheduler],
) -> None:
    s = make(db_path, deliveries, clock, sleep)
    with pytest.raises(ValueError):
        s.schedule("", "u1", {}, due_at=1.0)
    with pytest.raises(ValueError):
        s.schedule("k", "", {}, due_at=1.0)
    with pytest.raises(TypeError):
        s.schedule("k", "u1", {"obj": object()}, due_at=1.0)  # type: ignore[dict-item]
    with pytest.raises(TypeError):
        s.schedule("k", "u1", [], due_at=1.0)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        s.schedule("k", "u1", {}, due_at=math.nan)
    with pytest.raises(TypeError):
        s.schedule("k", "u1", {}, due_at=True)  # type: ignore[arg-type]
    assert s.pending() == []


async def test_scheduler_shares_database_file_with_session_store(
    db_path: Path,
    clock: FakeClock,
    sleep: FakeSleep,
    deliveries: Deliveries,
) -> None:
    store = SessionStore(db_path)
    s = Scheduler(db_path, deliveries, clock=clock, sleep=sleep)
    try:
        store.append("private:u1", "user", {"text": "hi"})
        eid = s.schedule(KIND_COMPACTION_IDLE, "u1", {}, due_at=clock.now + EIGHT_HOURS)
        items = store.load("private:u1")
        assert [(i.seq, i.kind) for i in items] == [(1, "user")]
        assert [e.event_id for e in s.pending()] == [eid]
    finally:
        s.close()
        store.close()


async def test_system_event_queue_is_fifo_and_get_waits_for_put() -> None:
    q = SystemEventQueue()
    e1 = SystemEvent("e1", KIND_BACKGROUND_NOTICE, "u1", {"n": 1}, 1.0)
    e2 = SystemEvent("e2", KIND_BACKGROUND_NOTICE, None, {"n": 2}, 2.0)
    e3 = SystemEvent("e3", KIND_COMPACTION_IDLE, "u2", {"n": 3}, 3.0)
    q.put(e1)
    q.put(e2)
    assert q.qsize() == 2
    assert await q.get() is e1
    assert await q.get() is e2

    waiter = asyncio.ensure_future(q.get())
    await settle()
    assert not waiter.done()
    q.put(e3)
    assert await asyncio.wait_for(waiter, TIMEOUT) is e3
    assert q.qsize() == 0
    with pytest.raises(TypeError):
        q.put("not an event")  # type: ignore[arg-type]
