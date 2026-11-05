from __future__ import annotations

import math
import time
from pathlib import Path

import pytest
from compaction_fakes import SID, TZ, FakeProvider, Rows

from cgbot.compaction import IDLE_SECONDS, Compactor
from cgbot.config.settings import ReasoningTiers
from cgbot.scheduler import KIND_COMPACTION_IDLE, Scheduler, SystemEvent
from cgbot.session import AsyncSessionStore


class FakeClock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


async def _noop(_event: SystemEvent) -> None:
    return None


def _pending_idle(scheduler: Scheduler) -> list[SystemEvent]:
    return [e for e in scheduler.pending() if e.kind == KIND_COMPACTION_IDLE]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(time.time())


@pytest.fixture
def scheduler(tmp_path: Path, clock: FakeClock) -> Scheduler:
    return Scheduler(tmp_path / "scheduler.sqlite", _noop, clock=clock)


def _compactor(
    provider: FakeProvider,
    store: AsyncSessionStore,
    scheduler: Scheduler,
    clock: FakeClock,
    *,
    busy: bool = False,
) -> Compactor:
    return Compactor(
        provider,
        store,
        scheduler,
        ReasoningTiers(),
        is_busy=lambda _sid: busy,
        tz=TZ,
        clock=clock,
    )


def _event(due_at: float, session_id: str = SID) -> SystemEvent:
    return SystemEvent(
        event_id="evt",
        kind=KIND_COMPACTION_IDLE,
        user_id="u1",
        payload={"session_id": session_id},
        due_at=due_at,
    )


async def test_activity_arms_one_idle_event_and_moves_it_forward(
    store: AsyncSessionStore,
    scheduler: Scheduler,
    clock: FakeClock,
    provider_cls: type[FakeProvider],
) -> None:
    compactor = _compactor(provider_cls(), store, scheduler, clock)
    compactor.note_activity(SID)
    (first,) = _pending_idle(scheduler)
    assert math.isclose(first.due_at, clock.now + IDLE_SECONDS)
    assert first.payload == {"session_id": SID}
    assert first.user_id == "u1"

    clock.now += 100.0
    compactor.note_activity(SID)
    (moved,) = _pending_idle(scheduler)
    assert moved.event_id == first.event_id
    assert math.isclose(moved.due_at, clock.now + IDLE_SECONDS)


async def test_group_sessions_are_not_armed(
    store: AsyncSessionStore,
    scheduler: Scheduler,
    clock: FakeClock,
    provider_cls: type[FakeProvider],
) -> None:
    compactor = _compactor(provider_cls(), store, scheduler, clock)
    compactor.note_activity("group:42")
    assert _pending_idle(scheduler) == []


async def test_not_yet_idle_rearms_at_last_activity_plus_idle(
    store: AsyncSessionStore,
    scheduler: Scheduler,
    clock: FakeClock,
    rows_cls: type[Rows],
    provider_cls: type[FakeProvider],
) -> None:
    await rows_cls(store).exchange("你好", "你好呀")
    last = await store.last_activity(SID)
    assert last is not None
    clock.now = last + 3600.0
    provider = provider_cls()
    compactor = _compactor(provider, store, scheduler, clock)

    out = await compactor.handle_idle_event(_event(clock.now))
    assert out.status == "not_due"
    assert provider.requests == []
    (armed,) = _pending_idle(scheduler)
    assert math.isclose(armed.due_at, last + IDLE_SECONDS)


async def test_busy_session_is_rearmed_one_idle_period_later(
    store: AsyncSessionStore,
    scheduler: Scheduler,
    clock: FakeClock,
    rows_cls: type[Rows],
    provider_cls: type[FakeProvider],
) -> None:
    rows = Rows(store)
    for i in range(4):
        await rows.exchange(f"问{i}", f"答{i}")
    clock.now += IDLE_SECONDS + 1.0
    provider = provider_cls()
    compactor = _compactor(provider, store, scheduler, clock, busy=True)

    out = await compactor.handle_idle_event(_event(clock.now))
    assert out.status == "busy"
    assert provider.requests == []
    (armed,) = _pending_idle(scheduler)
    assert math.isclose(armed.due_at, clock.now + IDLE_SECONDS)


async def test_idle_event_compacts_regardless_of_threshold(
    store: AsyncSessionStore,
    scheduler: Scheduler,
    clock: FakeClock,
    rows_cls: type[Rows],
    provider_cls: type[FakeProvider],
) -> None:
    rows = Rows(store)
    for i in range(4):
        await rows.exchange(f"问{i}", f"答{i}")
    clock.now += IDLE_SECONDS + 1.0
    provider = provider_cls(context_window=10_000_000)
    compactor = _compactor(provider, store, scheduler, clock)

    out = await compactor.handle_idle_event(_event(clock.now))
    assert out.status == "compacted"
    assert out.reason == "idle"
    assert len(provider.requests) == 1
    assert _pending_idle(scheduler) == []

    rows = Rows(store)
    await rows.user("醒来后的第一句话")
    compactor.note_activity(SID)
    assert len(_pending_idle(scheduler)) == 1


async def test_session_without_rows_is_not_due(
    store: AsyncSessionStore,
    scheduler: Scheduler,
    clock: FakeClock,
    provider_cls: type[FakeProvider],
) -> None:
    provider = provider_cls()
    compactor = _compactor(provider, store, scheduler, clock)
    out = await compactor.handle_idle_event(_event(clock.now, "private:nobody"))
    assert out.status == "not_due"
    assert provider.requests == []


async def test_wrong_event_kind_is_rejected(
    store: AsyncSessionStore,
    scheduler: Scheduler,
    clock: FakeClock,
    provider_cls: type[FakeProvider],
) -> None:
    compactor = _compactor(provider_cls(), store, scheduler, clock)
    other = SystemEvent(
        event_id="evt",
        kind="something_else",
        user_id="u1",
        payload={"session_id": SID},
        due_at=clock.now,
    )
    with pytest.raises(ValueError):
        await compactor.handle_idle_event(other)
