"""SQLite persistence for the coding manager's instruction queue and running turns (R5.3).

Pending instructions survive restarts; running-turn rows let the manager reconcile turns that
were in flight when the process stopped. Same conventions as ``session/store.py``: WAL, FULL
synchronous, BEGIN IMMEDIATE transactions, and a ``schema_version`` row per component.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

SCHEMA_COMPONENT = "coding"
SCHEMA_VERSION = 1


class QueueStoreError(Exception):
    """Raised when the database schema is incompatible or unusable."""


@dataclass(frozen=True, slots=True)
class PendingJob:
    seq: int
    user_id: str
    thread_id: str
    prompt: str
    title: str | None
    created_at: float


@dataclass(frozen=True, slots=True)
class RunningTurn:
    user_id: str
    thread_id: str
    turn_id: str
    started_at: float


_SCHEMA: tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS schema_version ("
    "component TEXT PRIMARY KEY, version INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS coding_jobs ("
    "seq INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, "
    "thread_id TEXT NOT NULL, prompt TEXT NOT NULL, title TEXT, created_at REAL NOT NULL)",
    "CREATE INDEX IF NOT EXISTS coding_jobs_user ON coding_jobs(user_id, seq)",
    "CREATE TABLE IF NOT EXISTS coding_running ("
    "user_id TEXT NOT NULL, thread_id TEXT NOT NULL, turn_id TEXT NOT NULL, "
    "started_at REAL NOT NULL, PRIMARY KEY (user_id, thread_id))",
)


class QueueStore:
    """Thread-safe (single lock) sqlite store; each method is one short transaction."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._closed = False
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            with self._transaction() as conn:
                for statement in _SCHEMA:
                    conn.execute(statement)
                row = conn.execute(
                    "SELECT version FROM schema_version WHERE component = ?",
                    (SCHEMA_COMPONENT,),
                ).fetchone()
                if row is None:
                    conn.execute(
                        "INSERT INTO schema_version(component, version) VALUES (?, ?)",
                        (SCHEMA_COMPONENT, SCHEMA_VERSION),
                    )
                elif int(row["version"]) > SCHEMA_VERSION:
                    raise QueueStoreError("coding store schema is newer than this build")

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    def _check_open(self) -> None:
        if self._closed:
            raise QueueStoreError("coding store is closed")

    def enqueue(
        self,
        user_id: str,
        thread_id: str,
        prompt: str,
        title: str | None,
        created_at: float,
    ) -> int:
        """Append a pending instruction; returns its monotonically increasing sequence number."""
        with self._lock:
            self._check_open()
            with self._transaction() as conn:
                cur = conn.execute(
                    "INSERT INTO coding_jobs(user_id, thread_id, prompt, title, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (user_id, thread_id, prompt, title, created_at),
                )
                seq = cur.lastrowid
        if seq is None:
            raise QueueStoreError("insert returned no sequence number")
        return seq

    def pending(self) -> list[PendingJob]:
        """All pending instructions across users, oldest first."""
        with self._lock:
            self._check_open()
            rows = self._conn.execute(
                "SELECT seq, user_id, thread_id, prompt, title, created_at "
                "FROM coding_jobs ORDER BY seq"
            ).fetchall()
        return [
            PendingJob(
                seq=int(r["seq"]),
                user_id=str(r["user_id"]),
                thread_id=str(r["thread_id"]),
                prompt=str(r["prompt"]),
                title=None if r["title"] is None else str(r["title"]),
                created_at=float(r["created_at"]),
            )
            for r in rows
        ]

    def delete_jobs(self, seqs: Sequence[int]) -> None:
        """Remove pending instructions by sequence number (one transaction)."""
        if not seqs:
            return
        with self._lock:
            self._check_open()
            with self._transaction() as conn:
                conn.executemany("DELETE FROM coding_jobs WHERE seq = ?", [(s,) for s in seqs])

    def start_job(
        self,
        seq: int,
        user_id: str,
        thread_id: str,
        turn_id: str,
        started_at: float,
    ) -> None:
        """Atomically move a pending instruction to a running-turn record."""
        with self._lock:
            self._check_open()
            with self._transaction() as conn:
                conn.execute("DELETE FROM coding_jobs WHERE seq = ?", (seq,))
                conn.execute(
                    "INSERT OR REPLACE INTO coding_running"
                    "(user_id, thread_id, turn_id, started_at) VALUES (?, ?, ?, ?)",
                    (user_id, thread_id, turn_id, started_at),
                )

    def running_turns(self) -> list[RunningTurn]:
        with self._lock:
            self._check_open()
            rows = self._conn.execute(
                "SELECT user_id, thread_id, turn_id, started_at FROM coding_running"
            ).fetchall()
        return [
            RunningTurn(
                user_id=str(r["user_id"]),
                thread_id=str(r["thread_id"]),
                turn_id=str(r["turn_id"]),
                started_at=float(r["started_at"]),
            )
            for r in rows
        ]

    def finish_turn(self, user_id: str, thread_id: str) -> None:
        with self._lock:
            self._check_open()
            with self._transaction() as conn:
                conn.execute(
                    "DELETE FROM coding_running WHERE user_id = ? AND thread_id = ?",
                    (user_id, thread_id),
                )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._conn.close()
