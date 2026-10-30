"""OmpStore: owner isolation, bounded reports, notification flags, file modes, reopen."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from cgbot.omp.store import Machine, OmpStore, TrackedTask


def machine(owner: str = "u1", alias: str = "box", password: str = "pw-1") -> Machine:
    return Machine(
        owner=owner,
        alias=alias,
        host="127.0.0.1",
        port=8080,
        password=password,
        auth="cookie",
        added_at=1.0,
    )


def tracked(owner: str, task_id: str, alias: str = "box") -> TrackedTask:
    return TrackedTask(owner=owner, task_id=task_id, alias=alias, cwd="/w", created_at=1.0)


async def add_report(store: OmpStore, owner: str, task_id: str, summary: str, limit: int = 50):
    await store.add_report(
        owner=owner,
        task_id=task_id,
        alias="box",
        cwd="/w",
        kind="error",
        at=1.0,
        summary=summary,
        detail={"n": 1},
        context_percent=None,
        limit=limit,
    )


@pytest.fixture
async def store(tmp_path: Path):
    s = OmpStore(tmp_path / "omp.sqlite")
    yield s
    s.close()


async def test_machines_are_isolated_per_owner(store: OmpStore) -> None:
    await store.add_machine(machine(owner="u1"))
    assert await store.list_machines("u2") == []
    assert await store.get_machine("u2", "box") is None
    assert [m.alias for m in await store.list_machines("u1")] == ["box"]


async def test_add_machine_reports_replacement(store: OmpStore) -> None:
    assert await store.add_machine(machine()) is False
    replaced = Machine(
        owner="u1",
        alias="box",
        host="10.0.0.5",
        port=9000,
        password="pw-2",
        auth="none",
        added_at=2.0,
    )
    assert await store.add_machine(replaced) is True
    saved = await store.get_machine("u1", "box")
    assert saved is not None
    assert (saved.host, saved.port, saved.auth) == ("10.0.0.5", 9000, "none")


async def test_passwords_are_listed_only_for_their_owner(store: OmpStore) -> None:
    await store.add_machine(machine(owner="u1", password="pw-1"))
    await store.add_machine(machine(owner="u2", alias="other", password="pw-2"))
    assert await store.passwords("u1") == ["pw-1"]
    assert await store.passwords("u2") == ["pw-2"]
    assert await store.passwords("nobody") == []


def test_machine_repr_hides_password() -> None:
    assert "pw-1" not in repr(machine())


async def test_delete_machine_returns_and_removes_its_tasks(store: OmpStore) -> None:
    await store.add_machine(machine())
    await store.save_task(tracked("u1", "t1"))
    await store.save_task(tracked("u1", "t2"))
    await store.save_task(tracked("u1", "t3", alias="other"))
    removed = await store.delete_machine("u1", "box")
    assert sorted(removed) == ["t1", "t2"]
    assert await store.count_tasks("u1") == {"other": 1}
    assert await store.get_machine("u1", "box") is None


async def test_task_owned_by_others_detects_cross_owner_ids(store: OmpStore) -> None:
    await store.save_task(tracked("u1", "t1"))
    assert await store.task_owned_by_others("u2", "t1") is True
    assert await store.task_owned_by_others("u1", "t1") is False


async def test_reports_are_bounded_to_newest_per_owner(store: OmpStore) -> None:
    for i in range(1, 6):
        await add_report(store, "u1", "t1", f"r{i}", limit=3)
    unread = await store.unread_reports("u1")
    assert [r.summary for r in unread] == ["r3", "r4", "r5"]


async def test_unread_reports_filter_by_task_and_mark_read(store: OmpStore) -> None:
    await add_report(store, "u1", "t1", "a")
    await add_report(store, "u1", "t2", "b")
    only_t1 = await store.unread_reports("u1", "t1")
    assert [r.summary for r in only_t1] == ["a"]
    await store.mark_read("u1", [r.seq for r in only_t1])
    assert [r.summary for r in await store.unread_reports("u1")] == ["b"]


async def test_pending_notifications_are_marked_once(store: OmpStore) -> None:
    await add_report(store, "u1", "t1", "first")
    pending = await store.pending_notifications("u1")
    assert [r.summary for r in pending] == ["first"]
    await store.mark_notified("u1", [r.seq for r in pending])
    assert await store.pending_notifications("u1") == []
    await add_report(store, "u1", "t1", "second")
    assert [r.summary for r in await store.pending_notifications("u1")] == ["second"]
    assert await store.owners_with_pending_notifications() == ["u1"]


async def test_add_report_rejects_unknown_kind(store: OmpStore) -> None:
    with pytest.raises(ValueError):
        await store.add_report(
            owner="u1",
            task_id="t1",
            alias="box",
            cwd="/w",
            kind="bogus",
            at=1.0,
            summary="x",
            detail=None,
            context_percent=None,
            limit=5,
        )


async def test_add_machine_rejects_unknown_auth(store: OmpStore) -> None:
    bad = Machine(
        owner="u1", alias="box", host="h", port=1, password="p", auth="basic", added_at=1.0
    )
    with pytest.raises(ValueError):
        await store.add_machine(bad)


async def test_database_and_sidecar_files_are_private(tmp_path: Path) -> None:
    db = tmp_path / "omp.sqlite"
    s = OmpStore(db)
    await s.add_machine(machine())
    await add_report(s, "u1", "t1", "x")
    for name in ("omp.sqlite", "omp.sqlite-wal", "omp.sqlite-shm"):
        path = tmp_path / name
        if path.exists():
            assert stat.S_IMODE(path.stat().st_mode) == 0o600, name
    s.close()


async def test_state_survives_reopen(tmp_path: Path) -> None:
    db = tmp_path / "omp.sqlite"
    s = OmpStore(db)
    await s.add_machine(machine())
    task = tracked("u1", "t9")
    task.pending_ui = {"r1": {"method": "select", "options": ["A"]}}
    task.report_signatures = ["sig"]
    await s.save_task(task)
    s.close()

    reopened = OmpStore(db)
    try:
        assert [m.alias for m in await reopened.list_machines("u1")] == ["box"]
        [restored] = await reopened.all_tasks()
        assert restored.task_id == "t9"
        assert restored.pending_ui == {"r1": {"method": "select", "options": ["A"]}}
        assert restored.report_signatures == ["sig"]
    finally:
        reopened.close()
