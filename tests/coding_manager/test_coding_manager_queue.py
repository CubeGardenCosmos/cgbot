from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from manager_fakes import Rig, build, until

from cgbot.coding.manager import QUEUE_STARTED_TEXT


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    current = build(tmp_path / "coding.sqlite")
    await current.manager.start()
    yield current
    await current.manager.close()


async def test_one_turn_per_thread_in_fifo_order(rig: Rig) -> None:
    first = await rig.manager.submit("alice", "a")
    thread = first.thread_id
    assert (first.state, first.queue_position) == ("started", 0)
    second = await rig.manager.submit("alice", "b", thread_id=thread)
    third = await rig.manager.submit("alice", "c", thread_id=thread)
    assert (second.state, second.queue_position) == ("queued", 1)
    assert (third.state, third.queue_position) == ("queued", 2)

    sb = rig.server.user("alice")
    assert [p for _, p, _ in sb.started] == ["a"]
    rig.server.complete("alice", thread)
    await until(lambda: [p for _, p, _ in sb.started] == ["a", "b"])
    rig.server.complete("alice", thread)
    await until(lambda: len(sb.started) == 3)
    rig.server.complete("alice", thread)
    await until(lambda: rig.notifier.count("turn_completed") == 3)

    assert [p for _, p, _ in sb.started] == ["a", "b", "c"]
    assert not sb.active
    assert rig.store.pending() == []
    assert rig.notifier.count("queue_started") == 2


async def test_per_user_cap_queues_the_fifth_thread(rig: Rig) -> None:
    threads = []
    for i in range(4):
        result = await rig.manager.submit("alice", f"p{i}")
        assert result.state == "started"
        threads.append(result.thread_id)
    fifth = await rig.manager.submit("alice", "p4")
    assert (fifth.state, fifth.queue_position) == ("queued", 1)

    sb = rig.server.user("alice")
    assert len(sb.active) == 4
    rig.server.complete("alice", threads[0])
    await until(lambda: len(sb.started) == 5)
    assert sb.started[-1][1] == "p4"
    assert len(sb.active) == 4


async def test_queued_instruction_does_not_block_other_threads(rig: Rig) -> None:
    first = await rig.manager.submit("alice", "a1")
    await rig.manager.submit("alice", "a2", thread_id=first.thread_id)
    other = await rig.manager.submit("alice", "b1")
    assert other.state == "started"
    sb = rig.server.user("alice")
    assert [p for _, p, _ in sb.started] == ["a1", "b1"]
    assert len(sb.active) == 2


async def test_queued_and_running_state_is_persisted(rig: Rig) -> None:
    first = await rig.manager.submit("alice", "a1")
    await rig.manager.submit("alice", "a2", thread_id=first.thread_id)
    assert [(j.thread_id, j.prompt) for j in rig.store.pending()] == [(first.thread_id, "a2")]
    [running] = rig.store.running_turns()
    assert running.thread_id == first.thread_id
    assert running.turn_id == rig.server.user("alice").active[first.thread_id]


async def test_queue_started_notice_only_for_jobs_that_waited(rig: Rig) -> None:
    first = await rig.manager.submit("alice", "a1")
    await rig.manager.submit("alice", "a2", thread_id=first.thread_id)
    rig.server.complete("alice", first.thread_id)
    await until(lambda: rig.notifier.count("queue_started") == 1)
    [notice] = rig.notifier.of("queue_started")
    assert notice.text == QUEUE_STARTED_TEXT
    assert notice.thread_id == first.thread_id


async def test_manager_is_built_with_real_fakes(rig: Rig) -> None:
    assert rig.governor.per_user == 4
    assert rig.governor.limit() == 4
