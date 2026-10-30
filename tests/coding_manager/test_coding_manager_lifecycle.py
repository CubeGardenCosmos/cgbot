from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from manager_fakes import Rig, build, until

from cgbot.coding.manager import CodingError


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    current = build(tmp_path / "coding.sqlite")
    await current.manager.start()
    yield current
    await current.manager.close()


async def test_archive_and_delete_refuse_while_busy(rig: Rig) -> None:
    first = await rig.manager.submit("alice", "a1")
    await rig.manager.submit("alice", "a2", thread_id=first.thread_id)
    with pytest.raises(CodingError, match="busy"):
        await rig.manager.archive("alice", first.thread_id)
    with pytest.raises(CodingError, match="busy"):
        await rig.manager.delete("alice", first.thread_id)


async def test_delete_removes_idle_thread_and_its_memo(rig: Rig) -> None:
    result = await rig.manager.submit("alice", "task", title="t")
    rig.server.complete("alice", result.thread_id)
    await until(lambda: rig.notifier.count("turn_completed") == 1)
    await rig.manager.delete("alice", result.thread_id)
    assert await rig.manager.list_threads("alice") == []
    with pytest.raises(CodingError):
        await rig.manager.peek("alice", result.thread_id)


async def test_submit_after_close_is_refused(tmp_path: Path) -> None:
    rig = build(tmp_path / "coding.sqlite")
    await rig.manager.start()
    await rig.manager.close()
    with pytest.raises(CodingError):
        await rig.manager.submit("alice", "late")


async def test_close_is_idempotent_and_releases_sessions(rig: Rig) -> None:
    await rig.manager.submit("alice", "a1")
    assert rig.sandbox.open_count == 1
    await rig.manager.close()
    await rig.manager.close()
    assert rig.sandbox.open_count == 0


async def test_sandbox_failure_surfaces_as_coding_error(rig: Rig) -> None:
    rig.sandbox.fail = True
    with pytest.raises(CodingError):
        await rig.manager.submit("alice", "a1")
    assert rig.store.pending() == []
