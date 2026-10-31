"""Subscriber list and per-issue send ledger for the daily 世界信号 push (SQLite).

``subscribers`` holds internal user ids that asked for the digest in private chat. ``sent`` gets one
row per (user, issue) once an attempt was made, so a restart never resends and a failed attempt is
not retried on every poll.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from pathlib import Path
from typing import Final

SCHEMA_COMPONENT: Final = "news"
SCHEMA_VERSION: Final = 1

_SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS schema_version (
    component TEXT PRIMARY KEY,
    version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS subscribers (
    user_id TEXT PRIMARY KEY,
    subscribed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sent (
    user_id TEXT NOT NULL,
    issue_id TEXT NOT NULL,
    sent_at REAL NOT NULL,
    PRIMARY KEY (user_id, issue_id)
);
"""


class NewsStore:
    """SQLite-backed subscriber list and send ledger. Blocking calls run in worker threads."""

    def __init__(self, path: Path) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            path, timeout=5.0, isolation_level=None, check_same_thread=False
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.execute(
            "INSERT INTO schema_version (component, version) VALUES (?, ?) "
            "ON CONFLICT(component) DO UPDATE SET version = excluded.version",
            (SCHEMA_COMPONENT, SCHEMA_VERSION),
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _write(self, sql: str, params: tuple[object, ...]) -> int:
        with self._lock:
            return self._conn.execute(sql, params).rowcount

    def _rows(self, sql: str, params: tuple[object, ...] = ()) -> list[tuple[object, ...]]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    async def subscribe(self, user_id: str, *, now: float | None = None) -> bool:
        """Add a subscriber. Returns False when the user was already subscribed."""
        at = time.time() if now is None else now
        rows = await asyncio.to_thread(
            self._write,
            "INSERT OR IGNORE INTO subscribers (user_id, subscribed_at) VALUES (?, ?)",
            (user_id, at),
        )
        return rows > 0

    async def unsubscribe(self, user_id: str) -> bool:
        """Remove a subscriber. Returns False when the user was not subscribed."""
        rows = await asyncio.to_thread(
            self._write, "DELETE FROM subscribers WHERE user_id = ?", (user_id,)
        )
        return rows > 0

    async def is_subscribed(self, user_id: str) -> bool:
        rows = await asyncio.to_thread(
            self._rows, "SELECT 1 FROM subscribers WHERE user_id = ?", (user_id,)
        )
        return bool(rows)

    async def subscribers(self) -> list[str]:
        rows = await asyncio.to_thread(
            self._rows, "SELECT user_id FROM subscribers ORDER BY subscribed_at, user_id"
        )
        return [str(row[0]) for row in rows]

    async def was_sent(self, user_id: str, issue_id: str) -> bool:
        rows = await asyncio.to_thread(
            self._rows,
            "SELECT 1 FROM sent WHERE user_id = ? AND issue_id = ?",
            (user_id, issue_id),
        )
        return bool(rows)

    async def mark_sent(self, user_id: str, issue_id: str, *, now: float | None = None) -> None:
        at = time.time() if now is None else now
        await asyncio.to_thread(
            self._write,
            "INSERT OR IGNORE INTO sent (user_id, issue_id, sent_at) VALUES (?, ?, ?)",
            (user_id, issue_id, at),
        )
