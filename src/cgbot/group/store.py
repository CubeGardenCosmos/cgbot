"""SQLite state for group gating: per-group mute/proactive/follow-up state, the unhandled
message buffer, and recent bot-sent message ids (for reply-to-bot detection).

Own file via ``StoragePaths.for_module("group_state")``; never shared with other modules.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from cgbot.group.buffer import BufferedMessage

SCHEMA_COMPONENT = "group_state"
SCHEMA_VERSION = 1
BOT_MESSAGE_RETENTION = 500


class GroupStateError(Exception):
    """Raised when the group state database cannot be used safely."""


@dataclass(frozen=True, slots=True)
class GroupState:
    group_id: str
    muted_until: float | None = None
    last_proactive_at: float | None = None
    last_roll_hour: str | None = None
    followup_until: float | None = None


class GroupStateStore:
    def __init__(self, path: str | os.PathLike[str]) -> None:
        db_path = Path(path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
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
                "CREATE TABLE IF NOT EXISTS group_state ("
                "group_id TEXT PRIMARY KEY, "
                "muted_until REAL, "
                "last_proactive_at REAL, "
                "last_roll_hour TEXT, "
                "followup_until REAL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS group_buffer ("
                "seq INTEGER PRIMARY KEY AUTOINCREMENT, "
                "group_id TEXT NOT NULL, "
                "message_id TEXT NOT NULL, "
                "user_id TEXT NOT NULL, "
                "ts REAL NOT NULL, "
                "text TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS group_buffer_by_group ON group_buffer (group_id, seq)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS bot_message ("
                "seq INTEGER PRIMARY KEY AUTOINCREMENT, "
                "message_id TEXT NOT NULL, "
                "group_id TEXT NOT NULL, "
                "ts REAL NOT NULL)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS bot_message_by_id ON bot_message (message_id, seq)"
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
                raise GroupStateError(
                    f"group_state schema version {version} is newer than supported {SCHEMA_VERSION}"
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

    def get_state(self, group_id: str) -> GroupState:
        with self._lock, self._transaction() as conn:
            row = conn.execute(
                "SELECT muted_until, last_proactive_at, last_roll_hour, followup_until "
                "FROM group_state WHERE group_id = ?",
                (group_id,),
            ).fetchone()
        if row is None:
            return GroupState(group_id=group_id)
        return GroupState(
            group_id=group_id,
            muted_until=_opt_float(row[0]),
            last_proactive_at=_opt_float(row[1]),
            last_roll_hour=None if row[2] is None else str(row[2]),
            followup_until=_opt_float(row[3]),
        )

    def put_state(self, state: GroupState) -> None:
        with self._lock, self._transaction() as conn:
            conn.execute(
                "INSERT INTO group_state "
                "(group_id, muted_until, last_proactive_at, last_roll_hour, followup_until) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (group_id) DO UPDATE SET "
                "muted_until = excluded.muted_until, "
                "last_proactive_at = excluded.last_proactive_at, "
                "last_roll_hour = excluded.last_roll_hour, "
                "followup_until = excluded.followup_until",
                (
                    state.group_id,
                    state.muted_until,
                    state.last_proactive_at,
                    state.last_roll_hour,
                    state.followup_until,
                ),
            )

    def append_buffer(self, group_id: str, msg: BufferedMessage, capacity: int) -> None:
        """Append and keep only the newest ``capacity`` messages for the group."""
        if capacity < 1:
            raise ValueError("buffer capacity must be >= 1")
        with self._lock, self._transaction() as conn:
            conn.execute(
                "INSERT INTO group_buffer (group_id, message_id, user_id, ts, text) "
                "VALUES (?, ?, ?, ?, ?)",
                (group_id, msg.message_id, msg.user_id, msg.ts, msg.text),
            )
            conn.execute(
                "DELETE FROM group_buffer WHERE group_id = ? AND seq <= ("
                "SELECT seq FROM group_buffer WHERE group_id = ? "
                "ORDER BY seq DESC LIMIT 1 OFFSET ?)",
                (group_id, group_id, capacity),
            )

    def clear_buffer(self, group_id: str) -> None:
        with self._lock, self._transaction() as conn:
            conn.execute("DELETE FROM group_buffer WHERE group_id = ?", (group_id,))

    def buffer(self, group_id: str) -> tuple[BufferedMessage, ...]:
        with self._lock, self._transaction() as conn:
            rows = conn.execute(
                "SELECT message_id, user_id, ts, text FROM group_buffer "
                "WHERE group_id = ? ORDER BY seq",
                (group_id,),
            ).fetchall()
        return tuple(
            BufferedMessage(message_id=str(r[0]), user_id=str(r[1]), ts=float(r[2]), text=str(r[3]))
            for r in rows
        )

    def record_bot_message(self, message_id: str, group_id: str, ts: float) -> None:
        with self._lock, self._transaction() as conn:
            conn.execute(
                "INSERT INTO bot_message (message_id, group_id, ts) VALUES (?, ?, ?)",
                (message_id, group_id, ts),
            )
            conn.execute(
                "DELETE FROM bot_message WHERE seq <= ("
                "SELECT seq FROM bot_message ORDER BY seq DESC LIMIT 1 OFFSET ?)",
                (BOT_MESSAGE_RETENTION,),
            )

    def bot_message_group(self, message_id: str) -> str | None:
        with self._lock, self._transaction() as conn:
            row = conn.execute(
                "SELECT group_id FROM bot_message WHERE message_id = ? ORDER BY seq DESC LIMIT 1",
                (message_id,),
            ).fetchone()
        return None if row is None else str(row[0])


def _opt_float(value: object) -> float | None:
    return None if value is None else float(str(value))
