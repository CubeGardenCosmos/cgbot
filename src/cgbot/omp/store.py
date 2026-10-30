"""SQLite store for the omp_* tools (R5.7): saved ompweb machines, tracked tasks, reports.

Owns ``omp.sqlite`` (``StoragePaths.for_module("omp")``). Every row is keyed by the internal
owner id (``user_id``), so one user never sees another user's machines, tasks or reports.

Machine passwords are stored in this file. The file and its SQLite sidecars are kept at mode
0600. Only :meth:`OmpStore.passwords` returns passwords, and only for redaction.

Concurrency follows the other stores: one connection guarded by a lock, writes in
``BEGIN IMMEDIATE``, and every SQLite call runs in a worker thread via ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_COMPONENT = "omp"
SCHEMA_VERSION = 1
MACHINE_AUTHS = ("cookie", "none")
_REPORT_KINDS = ("completed", "error", "needs_input", "exited", "progress")


class OmpStoreError(Exception):
    """Store-level failure: schema newer than this code, or an invalid stored row."""


@dataclass(frozen=True, slots=True)
class Machine:
    """A saved ompweb endpoint. ``password`` is kept out of ``repr``."""

    owner: str
    alias: str
    host: str
    port: int
    password: str = field(repr=False)
    auth: str
    added_at: float


@dataclass(slots=True)
class TrackedTask:
    """A session this module started or was asked to follow, plus its watcher bookkeeping."""

    owner: str
    task_id: str
    alias: str
    cwd: str
    created_at: float
    active: bool = False
    failed: bool = False
    waiting_input: bool = False
    process_seen: bool = False
    last_progress_at: float = 0.0
    last_report_state: str | None = None
    report_signatures: list[str] = field(default_factory=list)
    pending_ui: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Report:
    """A buffered background report. ``detail`` is already redacted when stored."""

    seq: int
    owner: str
    task_id: str
    alias: str
    cwd: str
    kind: str
    at: float
    summary: str
    detail: Any
    context_percent: float | None
    read: bool
    notified: bool


def _secure_file(path: Path) -> None:
    """Create ``path`` with mode 0600 if missing; force 0600 on an existing file."""
    if not path.exists():
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        os.close(fd)
    os.chmod(path, 0o600)


def _machine_from_row(row: tuple[Any, ...]) -> Machine:
    owner, alias, host, port, password, auth, added_at = row
    if auth not in MACHINE_AUTHS:
        raise OmpStoreError(f"corrupt machine auth in store: {auth!r}")
    return Machine(
        owner=str(owner),
        alias=str(alias),
        host=str(host),
        port=int(port),
        password=str(password),
        auth=str(auth),
        added_at=float(added_at),
    )


def _task_from_row(row: tuple[Any, ...]) -> TrackedTask:
    (
        owner,
        task_id,
        alias,
        cwd,
        created_at,
        active,
        failed,
        waiting_input,
        process_seen,
        last_progress_at,
        last_report_state,
        report_signatures,
        pending_ui,
    ) = row
    return TrackedTask(
        owner=str(owner),
        task_id=str(task_id),
        alias=str(alias),
        cwd=str(cwd),
        created_at=float(created_at),
        active=bool(active),
        failed=bool(failed),
        waiting_input=bool(waiting_input),
        process_seen=bool(process_seen),
        last_progress_at=float(last_progress_at),
        last_report_state=None if last_report_state is None else str(last_report_state),
        report_signatures=list(json.loads(report_signatures)),
        pending_ui=dict(json.loads(pending_ui)),
    )


def _report_from_row(row: tuple[Any, ...]) -> Report:
    (
        seq,
        owner,
        task_id,
        alias,
        cwd,
        kind,
        at,
        summary,
        detail,
        context_percent,
        read,
        notified,
    ) = row
    return Report(
        seq=int(seq),
        owner=str(owner),
        task_id=str(task_id),
        alias=str(alias),
        cwd=str(cwd),
        kind=str(kind),
        at=float(at),
        summary=str(summary),
        detail=json.loads(detail),
        context_percent=None if context_percent is None else float(context_percent),
        read=bool(read),
        notified=bool(notified),
    )


_REPORT_COLUMNS = (
    "seq, owner, task_id, alias, cwd, kind, at, summary, detail, context_percent, read, notified"
)
_TASK_COLUMNS = (
    "owner, task_id, alias, cwd, created_at, active, failed, waiting_input, process_seen, "
    "last_progress_at, last_report_state, report_signatures, pending_ui"
)


class OmpStore:
    """Async facade over one SQLite file holding machines, tasks and reports for omp_* tools."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        db_path = Path(path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        for path in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
            _secure_file(path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            db_path, timeout=5.0, isolation_level=None, check_same_thread=False
        )
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._init_schema()
        except BaseException:
            self._conn.close()
            raise
        for sidecar in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
            if sidecar.exists():
                os.chmod(sidecar, 0o600)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _init_schema(self) -> None:
        with self._transaction() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version "
                "(component TEXT PRIMARY KEY, version INTEGER NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS machines ("
                "owner TEXT NOT NULL, alias TEXT NOT NULL, host TEXT NOT NULL, "
                "port INTEGER NOT NULL, password TEXT NOT NULL, auth TEXT NOT NULL, "
                "added_at REAL NOT NULL, PRIMARY KEY (owner, alias))"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS tasks ("
                "owner TEXT NOT NULL, task_id TEXT NOT NULL, alias TEXT NOT NULL, "
                "cwd TEXT NOT NULL, created_at REAL NOT NULL, "
                "active INTEGER NOT NULL, failed INTEGER NOT NULL, "
                "waiting_input INTEGER NOT NULL, process_seen INTEGER NOT NULL, "
                "last_progress_at REAL NOT NULL, last_report_state TEXT, "
                "report_signatures TEXT NOT NULL, pending_ui TEXT NOT NULL, "
                "PRIMARY KEY (owner, task_id))"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS tasks_by_alias ON tasks (owner, alias)")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS reports ("
                "seq INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL, "
                "task_id TEXT NOT NULL, alias TEXT NOT NULL, cwd TEXT NOT NULL, "
                "kind TEXT NOT NULL, at REAL NOT NULL, summary TEXT NOT NULL, "
                "detail TEXT NOT NULL, context_percent REAL, "
                "read INTEGER NOT NULL DEFAULT 0, notified INTEGER NOT NULL DEFAULT 0)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS reports_by_owner ON reports (owner, seq)")
            conn.execute(
                "INSERT INTO schema_version (component, version) VALUES (?, ?) "
                "ON CONFLICT (component) DO NOTHING",
                (SCHEMA_COMPONENT, SCHEMA_VERSION),
            )
            row = conn.execute(
                "SELECT version FROM schema_version WHERE component = ?", (SCHEMA_COMPONENT,)
            ).fetchone()
            version = int(row[0])
            if version > SCHEMA_VERSION:
                raise OmpStoreError(
                    f"omp schema version {version} is newer than supported {SCHEMA_VERSION}"
                )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    # -- machines -----------------------------------------------------------------------------

    async def add_machine(self, machine: Machine) -> bool:
        """Insert or replace ``(owner, alias)``. Returns True if an existing record was replaced."""
        if machine.auth not in MACHINE_AUTHS:
            raise ValueError("invalid machine auth")
        return await asyncio.to_thread(self._add_machine, machine)

    def _add_machine(self, machine: Machine) -> bool:
        with self._lock, self._transaction() as conn:
            existed = (
                conn.execute(
                    "SELECT 1 FROM machines WHERE owner = ? AND alias = ?",
                    (machine.owner, machine.alias),
                ).fetchone()
                is not None
            )
            conn.execute(
                "INSERT INTO machines (owner, alias, host, port, password, auth, added_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (owner, alias) DO UPDATE SET host = excluded.host, "
                "port = excluded.port, password = excluded.password, auth = excluded.auth, "
                "added_at = excluded.added_at",
                (
                    machine.owner,
                    machine.alias,
                    machine.host,
                    machine.port,
                    machine.password,
                    machine.auth,
                    machine.added_at,
                ),
            )
            return existed

    async def get_machine(self, owner: str, alias: str) -> Machine | None:
        return await asyncio.to_thread(self._get_machine, owner, alias)

    def _get_machine(self, owner: str, alias: str) -> Machine | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT owner, alias, host, port, password, auth, added_at FROM machines "
                "WHERE owner = ? AND alias = ?",
                (owner, alias),
            ).fetchone()
        return None if row is None else _machine_from_row(row)

    async def list_machines(self, owner: str) -> list[Machine]:
        return await asyncio.to_thread(self._list_machines, owner)

    def _list_machines(self, owner: str) -> list[Machine]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT owner, alias, host, port, password, auth, added_at FROM machines "
                "WHERE owner = ? ORDER BY rowid",
                (owner,),
            ).fetchall()
        return [_machine_from_row(row) for row in rows]

    async def delete_machine(self, owner: str, alias: str) -> list[str]:
        """Remove the machine and its tracked tasks. Returns the removed task ids."""
        return await asyncio.to_thread(self._delete_machine, owner, alias)

    def _delete_machine(self, owner: str, alias: str) -> list[str]:
        with self._lock, self._transaction() as conn:
            rows = conn.execute(
                "SELECT task_id FROM tasks WHERE owner = ? AND alias = ?", (owner, alias)
            ).fetchall()
            conn.execute("DELETE FROM tasks WHERE owner = ? AND alias = ?", (owner, alias))
            conn.execute("DELETE FROM machines WHERE owner = ? AND alias = ?", (owner, alias))
        return [str(row[0]) for row in rows]

    async def passwords(self, owner: str) -> list[str]:
        """Saved passwords of one owner. Used only to redact outputs and logs."""
        return await asyncio.to_thread(self._passwords, owner)

    def _passwords(self, owner: str) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT password FROM machines WHERE owner = ?", (owner,)
            ).fetchall()
        return [str(row[0]) for row in rows if row[0]]

    # -- tasks --------------------------------------------------------------------------------

    async def save_task(self, task: TrackedTask) -> None:
        await asyncio.to_thread(self._save_task, task)

    def _save_task(self, task: TrackedTask) -> None:
        with self._lock, self._transaction() as conn:
            conn.execute(
                f"INSERT INTO tasks ({_TASK_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (owner, task_id) DO UPDATE SET alias = excluded.alias, "
                "cwd = excluded.cwd, created_at = excluded.created_at, "
                "active = excluded.active, failed = excluded.failed, "
                "waiting_input = excluded.waiting_input, "
                "process_seen = excluded.process_seen, "
                "last_progress_at = excluded.last_progress_at, "
                "last_report_state = excluded.last_report_state, "
                "report_signatures = excluded.report_signatures, "
                "pending_ui = excluded.pending_ui",
                (
                    task.owner,
                    task.task_id,
                    task.alias,
                    task.cwd,
                    task.created_at,
                    int(task.active),
                    int(task.failed),
                    int(task.waiting_input),
                    int(task.process_seen),
                    task.last_progress_at,
                    task.last_report_state,
                    json.dumps(task.report_signatures),
                    json.dumps(task.pending_ui, ensure_ascii=False),
                ),
            )

    async def all_tasks(self) -> list[TrackedTask]:
        return await asyncio.to_thread(self._all_tasks)

    def _all_tasks(self) -> list[TrackedTask]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_TASK_COLUMNS} FROM tasks ORDER BY rowid"
            ).fetchall()
        return [_task_from_row(row) for row in rows]

    async def task_owned_by_others(self, owner: str, task_id: str) -> bool:
        return await asyncio.to_thread(self._task_owned_by_others, owner, task_id)

    def _task_owned_by_others(self, owner: str, task_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM tasks WHERE task_id = ? AND owner != ?", (task_id, owner)
            ).fetchone()
        return row is not None

    async def count_tasks(self, owner: str) -> dict[str, int]:
        return await asyncio.to_thread(self._count_tasks, owner)

    def _count_tasks(self, owner: str) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT alias, COUNT(*) FROM tasks WHERE owner = ? GROUP BY alias", (owner,)
            ).fetchall()
        return {str(alias): int(count) for alias, count in rows}

    # -- reports ------------------------------------------------------------------------------

    async def add_report(
        self,
        *,
        owner: str,
        task_id: str,
        alias: str,
        cwd: str,
        kind: str,
        at: float,
        summary: str,
        detail: Any,
        context_percent: float | None,
        limit: int,
    ) -> None:
        """Append a report and keep only the newest ``limit`` reports of this owner."""
        if kind not in _REPORT_KINDS:
            raise ValueError(f"unknown report kind: {kind!r}")
        if limit < 1:
            raise ValueError("report buffer limit must be >= 1")
        await asyncio.to_thread(
            self._add_report,
            owner,
            task_id,
            alias,
            cwd,
            kind,
            at,
            summary,
            json.dumps(detail, ensure_ascii=False),
            context_percent,
            limit,
        )

    def _add_report(
        self,
        owner: str,
        task_id: str,
        alias: str,
        cwd: str,
        kind: str,
        at: float,
        summary: str,
        detail: str,
        context_percent: float | None,
        limit: int,
    ) -> None:
        with self._lock, self._transaction() as conn:
            conn.execute(
                "INSERT INTO reports (owner, task_id, alias, cwd, kind, at, summary, detail, "
                "context_percent) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (owner, task_id, alias, cwd, kind, at, summary, detail, context_percent),
            )
            conn.execute(
                "DELETE FROM reports WHERE owner = ? AND seq NOT IN ("
                "SELECT seq FROM reports WHERE owner = ? ORDER BY seq DESC LIMIT ?)",
                (owner, owner, limit),
            )

    async def unread_reports(self, owner: str, task_id: str | None = None) -> list[Report]:
        return await asyncio.to_thread(self._unread_reports, owner, task_id)

    def _unread_reports(self, owner: str, task_id: str | None) -> list[Report]:
        with self._lock:
            if task_id is None:
                rows = self._conn.execute(
                    f"SELECT {_REPORT_COLUMNS} FROM reports WHERE owner = ? AND read = 0 "
                    "ORDER BY seq",
                    (owner,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    f"SELECT {_REPORT_COLUMNS} FROM reports WHERE owner = ? AND read = 0 "
                    "AND task_id = ? ORDER BY seq",
                    (owner, task_id),
                ).fetchall()
        return [_report_from_row(row) for row in rows]

    async def mark_read(self, owner: str, seqs: Sequence[int]) -> None:
        if seqs:
            await asyncio.to_thread(self._mark_flag, owner, "read", list(seqs))

    async def pending_notifications(self, owner: str) -> list[Report]:
        return await asyncio.to_thread(self._pending_notifications, owner)

    def _pending_notifications(self, owner: str) -> list[Report]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_REPORT_COLUMNS} FROM reports WHERE owner = ? AND notified = 0 "
                "ORDER BY seq",
                (owner,),
            ).fetchall()
        return [_report_from_row(row) for row in rows]

    async def mark_notified(self, owner: str, seqs: Sequence[int]) -> None:
        if seqs:
            await asyncio.to_thread(self._mark_flag, owner, "notified", list(seqs))

    def _mark_flag(self, owner: str, column: str, seqs: list[int]) -> None:
        if column not in ("read", "notified"):
            raise ValueError("unknown report flag")
        marks = ", ".join("?" for _ in seqs)
        with self._lock, self._transaction() as conn:
            conn.execute(
                f"UPDATE reports SET {column} = 1 WHERE owner = ? AND seq IN ({marks})",
                (owner, *seqs),
            )

    async def owners_with_pending_notifications(self) -> list[str]:
        return await asyncio.to_thread(self._owners_with_pending_notifications)

    def _owners_with_pending_notifications(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT owner FROM reports WHERE notified = 0 ORDER BY owner"
            ).fetchall()
        return [str(row[0]) for row in rows]
