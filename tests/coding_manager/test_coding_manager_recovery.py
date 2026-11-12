from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest
from manager_fakes import FakeClock, FakeCodexServer, Rig, build, settle, until

from cgbot.coding.manager import VANISHED_TEXT

RigFactory = Callable[..., Rig]


@pytest.fixture
async def make_rig(tmp_path: Path) -> AsyncIterator[RigFactory]:
    """Build managers over one shared store path; callers share clock and server."""
    rigs: list[Rig] = []

    def make(
        clock: FakeClock | None = None,
        server: FakeCodexServer | None = None,
        reconcile_timeout: float = 30.0,
    ) -> Rig:
        rig = build(
            tmp_path / "coding.sqlite",
            clock=clock,
            server=server,
            reconcile_timeout=reconcile_timeout,
        )
        rigs.append(rig)
        return rig

    yield make
    for rig in rigs:
        await rig.manager.close()


async def test_restart_restores_queue_and_running_turn(make_rig: RigFactory) -> None:
    first = make_rig()
    await first.manager.start()
    result = await first.manager.submit("alice", "a1")
    await first.manager.submit("alice", "a2", thread_id=result.thread_id)
    await first.manager.close()

    second = make_rig(clock=first.clock, server=first.server)
    await second.manager.start()
    sb = first.server.user("alice")
    # The restored queue entry must wait for the restored running turn, not start a second one.
    await settle()
    assert [p for _, p, _ in sb.started] == ["a1"]
    assert [j.prompt for j in second.store.pending()] == ["a2"]

    first.server.complete("alice", result.thread_id)
    await until(lambda: [p for _, p, _ in sb.started] == ["a1", "a2"])
    await until(lambda: second.notifier.count("turn_completed") == 1)
    assert second.store.pending() == []


async def test_reconciled_active_turn_is_kept_not_failed(make_rig: RigFactory) -> None:
    first = make_rig()
    await first.manager.start()
    result = await first.manager.submit("alice", "long")
    await first.manager.close()

    first.server.user("alice").resume_status = "active"
    second = make_rig(clock=first.clock, server=first.server)
    await second.manager.start()
    await settle()
    first.clock.advance(120.0)
    await settle()

    assert second.notifier.count("turn_failed") == 0
    assert [r.thread_id for r in second.store.running_turns()] == [result.thread_id]
    first.server.complete("alice", result.thread_id)
    await until(lambda: second.notifier.count("turn_completed") == 1)


async def test_running_turn_vanishes_once_when_status_is_idle(make_rig: RigFactory) -> None:
    first = make_rig()
    await first.manager.start()
    result = await first.manager.submit("alice", "long")
    await first.manager.close()

    first.server.user("alice").resume_status = "idle"
    second = make_rig(clock=first.clock, server=first.server)
    await second.manager.start()
    await until(lambda: second.notifier.count("turn_failed") == 1)
    [notice] = second.notifier.of("turn_failed")
    assert notice.text == VANISHED_TEXT
    assert notice.thread_id == result.thread_id
    assert second.store.running_turns() == []
    first.clock.advance(120.0)
    await settle()
    assert second.notifier.count("turn_failed") == 1


async def test_running_turn_vanishes_on_reconcile_timeout(make_rig: RigFactory) -> None:
    first = make_rig()
    await first.manager.start()
    await first.manager.submit("alice", "long")
    await first.manager.close()

    # No thread_status is emitted; the reconcile window must expire on the clock.
    second = make_rig(clock=first.clock, server=first.server, reconcile_timeout=30.0)
    await second.manager.start()
    await settle()
    second.clock.advance(29.0)
    await settle()
    assert second.notifier.count("turn_failed") == 0
    second.clock.advance(1.0)
    await until(lambda: second.notifier.count("turn_failed") == 1)
    assert second.store.running_turns() == []


async def test_resume_failure_marks_running_turn_failed(make_rig: RigFactory) -> None:
    first = make_rig()
    await first.manager.start()
    await first.manager.submit("alice", "long")
    await first.manager.close()

    first.server.user("alice").resume_fails = True
    second = make_rig(clock=first.clock, server=first.server)
    await second.manager.start()
    await until(lambda: second.notifier.count("turn_failed") == 1)
    assert second.store.running_turns() == []


async def test_restart_with_nothing_persisted_is_a_no_op(make_rig: RigFactory) -> None:
    rig = make_rig()
    await rig.manager.start()
    assert rig.notifier.notices == []
    assert await rig.manager.list_threads("alice") == []
