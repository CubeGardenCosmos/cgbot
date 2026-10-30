from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from manager_fakes import Rig, build, until

from cgbot.coding.manager import DEFAULT_STOPPED_TEXT, CodingError


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    current = build(tmp_path / "coding.sqlite")
    await current.manager.start()
    yield current
    await current.manager.close()


async def test_steer_sends_text_to_the_running_turn(rig: Rig) -> None:
    result = await rig.manager.submit("alice", "long task")
    await rig.manager.steer("alice", result.thread_id, "also check tests")
    sb = rig.server.user("alice")
    assert sb.steers == [(result.thread_id, sb.started[0][2], "also check tests")]


async def test_steer_rejects_blank_text_and_idle_thread(rig: Rig) -> None:
    result = await rig.manager.submit("alice", "task")
    with pytest.raises(CodingError):
        await rig.manager.steer("alice", result.thread_id, "   ")
    rig.server.complete("alice", result.thread_id)
    await until(lambda: rig.notifier.count("turn_completed") == 1)
    with pytest.raises(CodingError):
        await rig.manager.steer("alice", result.thread_id, "late")


async def test_stop_interrupts_and_keeps_queue_by_default(rig: Rig) -> None:
    first = await rig.manager.submit("alice", "a1")
    await rig.manager.submit("alice", "a2", thread_id=first.thread_id)
    dropped = await rig.manager.stop("alice", first.thread_id)
    assert dropped == 0
    sb = rig.server.user("alice")
    assert len(sb.interrupts) == 1
    await until(lambda: rig.notifier.count("turn_interrupted") == 1)
    [notice] = rig.notifier.of("turn_interrupted")
    assert notice.text == DEFAULT_STOPPED_TEXT
    # The queued instruction starts after the interrupt.
    await until(lambda: [p for _, p, _ in sb.started] == ["a1", "a2"])


async def test_stop_with_clear_queue_drops_queued_instructions(rig: Rig) -> None:
    first = await rig.manager.submit("alice", "a1")
    await rig.manager.submit("alice", "a2", thread_id=first.thread_id)
    await rig.manager.submit("alice", "a3", thread_id=first.thread_id)
    dropped = await rig.manager.stop("alice", first.thread_id, clear_queue=True)
    assert dropped == 2
    assert rig.store.pending() == []
    await until(lambda: rig.notifier.count("turn_interrupted") == 1)
    sb = rig.server.user("alice")
    assert [p for _, p, _ in sb.started] == ["a1"]


async def test_stop_on_idle_thread_is_a_no_op(rig: Rig) -> None:
    result = await rig.manager.submit("alice", "task")
    rig.server.complete("alice", result.thread_id)
    await until(lambda: rig.notifier.count("turn_completed") == 1)
    assert await rig.manager.stop("alice", result.thread_id) == 0
    assert rig.server.user("alice").interrupts == []
