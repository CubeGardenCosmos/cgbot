"""Group meme/jargon table, independent of the group skill card.

Entries are written from the ``memes`` field of each group session summary (one entry per
``词: 解释`` string) and read by the ``meme_lookup`` tool. Every entry belongs to exactly one
group; scope is enforced here by ``group_id`` in SQL, never by the model.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

SCHEMA_COMPONENT = "meme"
SCHEMA_VERSION = 1

MAX_TERM_CHARS = 32
MAX_MEANING_CHARS = 200
MAX_ENTRIES_PER_GROUP = 300
MAX_QUERY_CHARS = 50
MAX_LOOKUP_RESULTS = 5

_SEPARATORS = (":", "：")


@dataclass(frozen=True, slots=True)
class MemeEntry:
    term: str
    meaning: str


def parse_meme(text: str) -> MemeEntry:
    """Split a ``词: 解释`` string on the first ASCII or full-width colon.

    Without a usable split the whole string is the term and the meaning is empty. Both parts
    are stripped and truncated to the table bounds.
    """
    stripped = " ".join(text.split())
    term, meaning = stripped, ""
    for sep in _SEPARATORS:
        if sep in stripped:
            head, _, tail = stripped.partition(sep)
            if head.strip() and len(head.strip()) <= MAX_TERM_CHARS:
                term, meaning = head.strip(), tail.strip()
            break
    return MemeEntry(
        term=term[:MAX_TERM_CHARS],
        meaning=meaning[:MAX_MEANING_CHARS],
    )


def _key(text: str) -> str:
    return text.casefold()


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _search_text(term: str, meaning: str) -> str:
    return f"{_key(term)}\n{_key(meaning)}"


class MemeStore:
    """SQLite-backed per-group meme table. Blocking calls run in worker threads."""

    def __init__(self, path: Path) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            path, timeout=5.0, isolation_level=None, check_same_thread=False
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version "
                "(component TEXT PRIMARY KEY, version INTEGER NOT NULL)"
            )
            row = self._conn.execute(
                "SELECT version FROM schema_version WHERE component = ?", (SCHEMA_COMPONENT,)
            ).fetchone()
            if row is not None and int(row[0]) > SCHEMA_VERSION:
                raise RuntimeError(
                    f"{SCHEMA_COMPONENT} schema newer than supported ({row[0]} > {SCHEMA_VERSION})"
                )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS meme_entries ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " group_id TEXT NOT NULL,"
                " term TEXT NOT NULL,"
                " term_key TEXT NOT NULL,"
                " meaning TEXT NOT NULL,"
                " search_text TEXT NOT NULL,"
                " updated_at REAL NOT NULL,"
                " UNIQUE (group_id, term_key))"
            )
            self._conn.execute(
                "INSERT INTO schema_version (component, version) VALUES (?, ?) "
                "ON CONFLICT(component) DO UPDATE SET version = excluded.version",
                (SCHEMA_COMPONENT, SCHEMA_VERSION),
            )

    async def upsert_many(
        self, group_id: str, entries: Iterable[MemeEntry], *, now: float | None = None
    ) -> None:
        batch = [e for e in entries if e.term]
        if not batch:
            return
        await asyncio.to_thread(self._upsert_sync, group_id, batch, now or time.time())

    def _upsert_sync(self, group_id: str, batch: list[MemeEntry], now: float) -> None:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for entry in batch:
                    key = _key(entry.term)
                    row = self._conn.execute(
                        "SELECT meaning FROM meme_entries WHERE group_id = ? AND term_key = ?",
                        (group_id, key),
                    ).fetchone()
                    # A repeat without a new explanation keeps the one already recorded.
                    meaning = entry.meaning or (str(row[0]) if row is not None else "")
                    self._conn.execute(
                        "INSERT INTO meme_entries "
                        "(group_id, term, term_key, meaning, search_text, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(group_id, term_key) DO UPDATE SET "
                        " term = excluded.term, meaning = excluded.meaning,"
                        " search_text = excluded.search_text, updated_at = excluded.updated_at",
                        (
                            group_id,
                            entry.term,
                            key,
                            meaning,
                            _search_text(entry.term, meaning),
                            now,
                        ),
                    )
                self._conn.execute(
                    "DELETE FROM meme_entries WHERE group_id = ? AND id NOT IN ("
                    " SELECT id FROM meme_entries WHERE group_id = ?"
                    " ORDER BY updated_at DESC, id DESC LIMIT ?)",
                    (group_id, group_id, MAX_ENTRIES_PER_GROUP),
                )
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    async def lookup(
        self, query: str, *, group_id: str | None, limit: int = MAX_LOOKUP_RESULTS
    ) -> tuple[MemeEntry, ...]:
        """Entries whose term or meaning contains ``query`` (case-insensitive).

        ``group_id=None`` searches every group. Exact term matches come first, then most
        recently updated.
        """
        needle = " ".join(query.split())
        return await asyncio.to_thread(self._lookup_sync, needle, group_id, limit)

    def _lookup_sync(self, needle: str, group_id: str | None, limit: int) -> tuple[MemeEntry, ...]:
        pattern = "%" + _escape_like(_key(needle)) + "%"
        conditions = ["search_text LIKE ? ESCAPE '\\'"]
        params: list[object] = [pattern]
        if group_id is not None:
            conditions.append("group_id = ?")
            params.append(group_id)
        sql = (
            "SELECT term, meaning FROM meme_entries WHERE "
            + " AND ".join(conditions)
            + " ORDER BY (term_key = ?) DESC, updated_at DESC, id DESC LIMIT ?"
        )
        params.extend([_key(needle), limit])
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return tuple(MemeEntry(term=str(t), meaning=str(m)) for t, m in rows)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
