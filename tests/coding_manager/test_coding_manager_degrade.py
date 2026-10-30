from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from manager_fakes import Rig, build, until

from cgbot.coding.manager import START_FAILED_TEXT


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    current = build(tmp_path / "coding.sqlite")
    await current.manager.start()
    yield current
    await current.manager.close()


async def test_degraded_limit_queues_new_threads_until_recovery(rig: Rig) -> None:
    for _ in range(3):
        rig.governor.on_upstream_status(503)
    assert rig.governor.limit() == 1

    first = await rig.manager.submit("alice", "a")
    second = await rig.manager.submit("alice", "b")
    assert first.state == "started"
    assert (second.state, second.queue_position) == ("queued", 1)

    rig.clock.advance(3599.0)
    assert rig.governor.limit() == 1
    rig.clock.advance(1.0)
    await until(lambda: rig.governor.limit() == 4)
    await until(lambda: len(rig.server.user("alice").started) == 2)


async def test_degradation_does_not_kill_running_turns(rig: Rig) -> None:
    first = await rig.manager.submit("alice", "a")
    for _ in range(3):
        rig.governor.on_upstream_status(0)
    assert rig.governor.degraded
    assert first.thread_id in rig.server.user("alice").active
    assert rig.notifier.count("turn_failed") == 0


async def test_429_during_degradation_postpones_recovery(rig: Rig) -> None:
    for _ in range(3):
        rig.governor.on_upstream_status(503)
    rig.clock.advance(3000.0)
    rig.governor.on_upstream_status(429)
    await rig.manager.submit("alice", "a")
    await rig.manager.submit("alice", "b")

    # Original degradation at t0 would recover at t0+3600; the 429 at t0+3000 pushes it out.
    rig.clock.advance(3599.0)
    assert rig.governor.limit() == 1
    rig.clock.advance(1.0)
    await until(lambda: rig.governor.limit() == 4)
    await until(lambda: len(rig.server.user("alice").started) == 2)


async def test_start_failure_is_retried_then_dropped_with_notice(rig: Rig) -> None:
    first = await rig.manager.submit("alice", "a1")
    await rig.manager.submit("alice", "a2", thread_id=first.thread_id)
    sb = rig.server.user("alice")
    sb.turn_start_failures = 3

    rig.server.complete("alice", first.thread_id)
    await until(lambda: rig.notifier.count("turn_failed") == 1)

    [notice] = rig.notifier.of("turn_failed")
    assert notice.text == START_FAILED_TEXT
    assert notice.thread_id == first.thread_id
    assert rig.store.pending() == []
    assert [p for _, p, _ in sb.started] == ["a1"]
    assert not sb.active
