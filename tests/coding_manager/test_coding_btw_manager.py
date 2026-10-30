from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from manager_fakes import Rig, build, until

from cgbot.coding.manager import PROGRESS_PREFIX, CodingError


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    current = build(tmp_path / "coding.sqlite")
    await current.manager.start()
    yield current
    await current.manager.close()


async def test_ask_progress_returns_side_answer_and_leaves_running_turn(rig: Rig) -> None:
    first = await rig.manager.submit("alice", "long task")
    sb = rig.server.user("alice")
    origin_turn = sb.started[0][2]
    task = asyncio.create_task(rig.manager.ask_progress("alice", first.thread_id, "进展如何"))
    await until(lambda: len(sb.started) == 2)
    fork_id, sent, _ = sb.started[1]
    assert fork_id != first.thread_id
    assert sent == PROGRESS_PREFIX + "进展如何"
    rig.server.complete("alice", fork_id, text="已完成一半")
    assert await task == "已完成一半"
    # The original turn is untouched, the fork is hidden and unsubscribed, nothing notified.
    assert sb.active == {first.thread_id: origin_turn}
    assert sb.interrupts == []
    assert sb.unsubscribed == [fork_id]
    assert fork_id not in [status.thread_id for status in await rig.manager.list_threads("alice")]
    assert rig.notifier.count("turn_completed") == 0


async def test_ask_progress_failed_side_turn_raises_and_cleans_up(rig: Rig) -> None:
    first = await rig.manager.submit("alice", "long task")
    sb = rig.server.user("alice")
    task = asyncio.create_task(rig.manager.ask_progress("alice", first.thread_id, "q"))
    await until(lambda: len(sb.started) == 2)
    fork_id = sb.started[1][0]
    rig.server.complete("alice", fork_id, status="failed", text=None)
    with pytest.raises(CodingError):
        await task
    assert sb.unsubscribed == [fork_id]


async def test_ask_progress_timeout_interrupts_side_turn(rig: Rig) -> None:
    first = await rig.manager.submit("alice", "long task")
    sb = rig.server.user("alice")
    with pytest.raises(CodingError, match="timed out"):
        await rig.manager.ask_progress("alice", first.thread_id, "q", wait_seconds=0.0)
    fork_id, _, side_turn = sb.started[1]
    assert sb.interrupts == [(fork_id, side_turn)]
    assert sb.unsubscribed == [fork_id]
    assert rig.notifier.count("turn_completed") == 0


async def test_ask_progress_unknown_thread_is_rejected(rig: Rig) -> None:
    with pytest.raises(CodingError, match="thread not found"):
        await rig.manager.ask_progress("alice", "no-such-thread", "q")
    assert rig.server.user("alice").started == []
