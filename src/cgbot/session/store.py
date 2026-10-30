"""Append-only session log in SQLite (R1.4).

- ``SessionStore``: synchronous core.
- ``AsyncSessionStore``: thin asyncio facade; every call runs the sync core via
  ``asyncio.to_thread`` so the event loop never blocks on SQLite I/O.

Concurrency: one connection per store, guarded by a ``threading.Lock`` (all reads and
writes take it). Writers in other processes are serialized by SQLite's write lock
(``BEGIN IMMEDIATE``).

Durability: WAL journal, ``synchronous=FULL``. Every ``append`` is its own committed
transaction; an append that has not committed is not visible after restart.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast, get_args

SCHEMA_COMPONENT = "session"
SCHEMA_VERSION = 1
DEFAULT_TOOL_OUTPUT_LIMIT = 64 * 1024

Kind = Literal["user", "assistant", "tool_call", "tool_result", "system_event", "compaction"]
KINDS: frozenset[str] = frozenset(get_args(Kind))

_SESSION_ID = re.compile(r"(?:private|group):\S+")
_SELECT = "SELECT seq, session_id, kind, payload, ts FROM session_items"


class SessionStoreError(Exception):
    """Store-level failure: schema newer than this code, or a corrupt stored row."""


@dataclass(frozen=True, slots=True)
class Item:
    seq: int
    session_id: str
    kind: Kind
    payload: dict[str, Any]
    ts: float


def _check_session_id(session_id: str) -> None:
    if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
        raise ValueError("invalid session id: expected 'private:<id>' or 'group:<id>'")


def _check_record(kind: str, payload: dict[str, Any]) -> None:
    if kind not in KINDS:
        raise ValueError(f"unknown session item kind: {kind!r}")
    if not isinstance(payload, dict):
        raise TypeError("payload must be a dict")
    if kind in ("tool_call", "tool_result"):
        call_id = payload.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            raise ValueError(f"{kind} payload requires a non-empty str call_id")
    if kind == "tool_result" and not isinstance(payload.get("output"), str):
        raise ValueError("tool_result payload requires a str output")
    if kind == "compaction" and not isinstance(payload.get("summary"), str):
        raise ValueError("compaction payload requires a str summary")


def _dump(payload: dict[str, Any]) -> str:
    # Raises TypeError/ValueError for non-JSON values (NaN, objects, ...).
    return json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _to_item(row: tuple[Any, ...]) -> Item:
    seq, session_id, kind, text, ts = row
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SessionStoreError(f"corrupt payload at seq {seq}") from exc
    return Item(seq=seq, session_id=session_id, kind=cast(Kind, kind), payload=payload, ts=ts)


class SessionStore:
    """SQLite-backed append-only session log.

    ``tool_output_limit``: ``tool_result`` output larger than this many UTF-8 bytes is
    truncated in the row (head + marker) and the full text is written to a new file in
    ``spill_dir`` (default ``<db dir>/spill``); the path is stored as ``spilled_path``.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        spill_dir: str | os.PathLike[str] | None = None,
        tool_output_limit: int = DEFAULT_TOOL_OUTPUT_LIMIT,
    ) -> None:
        if tool_output_limit < 1:
            raise ValueError("tool_output_limit must be >= 1")
        db_path = Path(path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        base_spill = Path(spill_dir) if spill_dir is not None else db_path.parent / "spill"
        self._spill_dir = base_spill.absolute()
        self._limit = tool_output_limit
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

    def _init_schema(self) -> None:
        with self._transaction() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version "
                "(component TEXT PRIMARY KEY, version INTEGER NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS session_items ("
                "seq INTEGER PRIMARY KEY AUTOINCREMENT, "
                "session_id TEXT NOT NULL, "
                "kind TEXT NOT NULL, "
                "payload TEXT NOT NULL, "
                "ts REAL NOT NULL)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS session_items_by_session "
                "ON session_items (session_id, seq)"
            )
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
                raise SessionStoreError(
                    f"session schema version {version} is newer than supported {SCHEMA_VERSION}"
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

    def append(self, session_id: str, kind: Kind, payload: dict[str, Any]) -> int:
        """Commit one record and return its globally increasing ``seq``."""
        _check_session_id(session_id)
        _check_record(kind, payload)
        text = _dump(payload)
        if kind == "tool_result":
            spilled = self._spill_if_large(payload)
            if spilled is not payload:
                text = _dump(spilled)
        with self._lock, self._transaction() as conn:
            cur = conn.execute(
                "INSERT INTO session_items (session_id, kind, payload, ts) VALUES (?, ?, ?, ?)",
                (session_id, kind, text, time.time()),
            )
            seq = cur.lastrowid
        if seq is None:
            raise SessionStoreError("insert returned no seq")
        return seq

    def append_if_last(
        self, session_id: str, expected_last_seq: int, kind: Kind, payload: dict[str, Any]
    ) -> int | None:
        """Like ``append``, but only if the session's newest seq is ``expected_last_seq``.

        The check and the insert share one ``BEGIN IMMEDIATE`` transaction, so no other
        append can land between them. Returns None (nothing written) on mismatch.
        Used by compaction to commit against the window it summarised (0 = empty session).
        """
        _check_session_id(session_id)
        _check_record(kind, payload)
        if kind == "tool_result":
            raise ValueError("append_if_last does not accept tool_result rows")
        text = _dump(payload)
        with self._lock, self._transaction() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM session_items WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if int(row[0]) != expected_last_seq:
                return None
            cur = conn.execute(
                "INSERT INTO session_items (session_id, kind, payload, ts) VALUES (?, ?, ?, ?)",
                (session_id, kind, text, time.time()),
            )
            seq = cur.lastrowid
        if seq is None:
            raise SessionStoreError("insert returned no seq")
        return seq

    def _spill_if_large(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw = cast(str, payload["output"]).encode("utf-8")
        if len(raw) <= self._limit:
            return payload
        self._spill_dir.mkdir(parents=True, exist_ok=True)
        spill_path = self._spill_dir / f"{uuid.uuid4().hex}.txt"
        with spill_path.open("xb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        dir_fd = os.open(self._spill_dir, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        head = raw[: self._limit].decode("utf-8", errors="ignore")
        marker = f"\n[truncated: {len(raw)} bytes total; full output in spilled_path]"
        return {
            **payload,
            "output": head + marker,
            "spilled_path": str(spill_path),
            "original_bytes": len(raw),
        }

    def load(self, session_id: str, after_seq: int = 0) -> list[Item]:
        """All items of the session with ``seq > after_seq``, in seq order."""
        _check_session_id(session_id)
        with self._lock:
            rows = self._conn.execute(
                _SELECT + " WHERE session_id = ? AND seq > ? ORDER BY seq",
                (session_id, after_seq),
            ).fetchall()
        return [_to_item(r) for r in rows]

    def load_active(self, session_id: str) -> list[Item]:
        """Items from the latest ``compaction`` onward (that compaction first).

        Without any compaction, returns the whole session. Items before the latest
        compaction stay on disk but are not part of the active window.
        """
        _check_session_id(session_id)
        with self._lock:
            rows = self._conn.execute(
                _SELECT
                + " WHERE session_id = ? AND seq >= COALESCE("
                + "(SELECT MAX(seq) FROM session_items"
                + " WHERE session_id = ? AND kind = 'compaction'), 0) ORDER BY seq",
                (session_id, session_id),
            ).fetchall()
        return [_to_item(r) for r in rows]

    def last_activity(self, session_id: str) -> float | None:
        """Unix time of the session's most recent item, or None if it has none."""
        _check_session_id(session_id)
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(ts) FROM session_items WHERE session_id = ?", (session_id,)
            ).fetchone()
        if row is None or row[0] is None:
            return None
        return float(row[0])

    def list_sessions(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT session_id FROM session_items ORDER BY session_id"
            ).fetchall()
        return [str(r[0]) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class AsyncSessionStore:
    """asyncio facade over ``SessionStore``: each call runs in a worker thread.

    Construction opens the database synchronously (startup-time cost only).
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        spill_dir: str | os.PathLike[str] | None = None,
        tool_output_limit: int = DEFAULT_TOOL_OUTPUT_LIMIT,
    ) -> None:
        self._store = SessionStore(path, spill_dir=spill_dir, tool_output_limit=tool_output_limit)

    async def append(self, session_id: str, kind: Kind, payload: dict[str, Any]) -> int:
        return await asyncio.to_thread(self._store.append, session_id, kind, payload)

    async def append_if_last(
        self, session_id: str, expected_last_seq: int, kind: Kind, payload: dict[str, Any]
    ) -> int | None:
        return await asyncio.to_thread(
            self._store.append_if_last, session_id, expected_last_seq, kind, payload
        )

    async def load(self, session_id: str, after_seq: int = 0) -> list[Item]:
        return await asyncio.to_thread(self._store.load, session_id, after_seq)

    async def load_active(self, session_id: str) -> list[Item]:
        return await asyncio.to_thread(self._store.load_active, session_id)

    async def last_activity(self, session_id: str) -> float | None:
        return await asyncio.to_thread(self._store.last_activity, session_id)

    async def list_sessions(self) -> list[str]:
        return await asyncio.to_thread(self._store.list_sessions)

    async def close(self) -> None:
        await asyncio.to_thread(self._store.close)
