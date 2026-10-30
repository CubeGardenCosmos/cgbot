from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from manager_fakes import Rig, build

from cgbot.coding.manager import CodingError


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    current = build(tmp_path / "coding.sqlite")
    await current.manager.start()
    yield current
    await current.manager.close()


async def test_user_cannot_read_another_users_thread(rig: Rig) -> None:
    mine = await rig.manager.submit("alice", "secret plan")
    with pytest.raises(CodingError):
        await rig.manager.peek("bob", mine.thread_id)
    with pytest.raises(CodingError):
        await rig.manager.read("bob", mine.thread_id)


async def test_user_cannot_steer_stop_or_mutate_another_users_thread(rig: Rig) -> None:
    mine = await rig.manager.submit("alice", "task")
    with pytest.raises(CodingError):
        await rig.manager.steer("bob", mine.thread_id, "hijack")
    with pytest.raises(CodingError):
        await rig.manager.stop("bob", mine.thread_id, clear_queue=True)
    with pytest.raises(CodingError):
        await rig.manager.rename("bob", mine.thread_id, "pwned")
    with pytest.raises(CodingError):
        await rig.manager.compact("bob", mine.thread_id)
    with pytest.raises(CodingError):
        await rig.manager.fork("bob", mine.thread_id)
    with pytest.raises(CodingError):
        await rig.manager.archive("bob", mine.thread_id)
    with pytest.raises(CodingError):
        await rig.manager.delete("bob", mine.thread_id)

    sb = rig.server.user("alice")
    assert sb.steers == []
    assert sb.interrupts == []
    assert sb.compacted == []
    assert sb.renamed == []
    assert mine.thread_id in sb.threads
    assert not sb.threads[mine.thread_id].archived


async def test_user_cannot_queue_onto_another_users_thread(rig: Rig) -> None:
    mine = await rig.manager.submit("alice", "task")
    with pytest.raises(CodingError):
        await rig.manager.submit("bob", "intrude", thread_id=mine.thread_id)
    assert rig.store.pending() == []
    assert [p for _, p, _ in rig.server.user("alice").started] == ["task"]


async def test_listings_contain_only_the_callers_threads(rig: Rig) -> None:
    alice = await rig.manager.submit("alice", "a")
    bob = await rig.manager.submit("bob", "b")
    alice_list = await rig.manager.list_threads("alice")
    bob_list = await rig.manager.list_threads("bob")
    assert [t.thread_id for t in alice_list] == [alice.thread_id]
    assert [t.thread_id for t in bob_list] == [bob.thread_id]


async def test_queued_work_is_scoped_to_its_user(rig: Rig) -> None:
    a1 = await rig.manager.submit("alice", "a1")
    await rig.manager.submit("alice", "a2", thread_id=a1.thread_id)
    b1 = await rig.manager.submit("bob", "b1")
    assert b1.state == "started"
    rig.server.complete("bob", b1.thread_id)
    assert [(j.user_id, j.prompt) for j in rig.store.pending()] == [("alice", "a2")]
