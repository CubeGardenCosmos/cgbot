"""Memory book storage in SQLite (R2.5).

Schema (component ``memory``, ``schema_version`` row kept like the other stores):

- ``memory_entries``: one row per entry; ``tags`` is the tag list joined by single spaces
  (tags never contain whitespace).
- ``memory_fts``: FTS5 index over ``text`` and ``tags`` with the ``trigram`` tokenizer. Present
  only when the SQLite build supports it; ``rowid`` equals ``memory_entries.id``.

Search backend: FTS5 trigram when available (default, auto-detected), otherwise ``LIKE``. Trigram
matches substrings of 3+ characters, which suits Chinese text that has no spaces. Query terms
shorter than 3 characters always use ``LIKE``. ``LIKE`` is ASCII case-insensitive only (SQLite),
so the fallback differs from FTS only for non-ASCII case folding. The backend is fixed per
database file: a file that already has ``memory_fts`` cannot be opened with FTS disabled.

Visibility, the hard constraint (DESIGN.md §7, AGENTS.md): every read, update and delete goes
through ``_visibility_clause``, which is applied in SQL *before* any ranking. Ranking happens in
Python over the already-filtered rows only, so invisible entries cannot influence order.

Write rule: ``domain`` must equal ``source_domain``. Any other combination is a cross-domain
promotion (private -> group, group A -> group B, group -> private) and is refused.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from cgbot.memory.domain import Viewer, check_domain, check_id

SCHEMA_COMPONENT = "memory"
SCHEMA_VERSION = 1

FTS_TABLE = "memory_fts"
MAX_TEXT_CHARS = 500
MAX_TAGS = 8
MAX_TAG_CHARS = 32
MAX_QUERY_CHARS = 200
MAX_QUERY_TERMS = 8
MIN_IMPORTANCE = 1
MAX_IMPORTANCE = 5
DEFAULT_IMPORTANCE = 3
DEFAULT_SEARCH_LIMIT = 8
_FTS_MIN_TERM_CHARS = 3

SearchBackend = Literal["fts5", "like"]


class MemoryBookError(Exception):
    """Base class for memory book errors."""


class CrossDomainWriteError(MemoryBookError):
    """A write tried to store material from one domain under another (promotion)."""


class MemoryStoreError(MemoryBookError):
    """Store-level failure: schema newer than supported, or an incompatible backend setting."""


@dataclass(frozen=True, slots=True)
class MemoryEntry:
    id: int
    subject_user: str
    domain: str
    source_domain: str
    text: str
    tags: tuple[str, ...]
    importance: int
    created_at: float
    updated_at: float


def format_entry(entry: MemoryEntry) -> str:
    """One prompt line for an entry, e.g. ``- [#12] text``."""
    return f"- [#{entry.id}] {entry.text}"


def format_entries(entries: Iterable[MemoryEntry]) -> str:
    return "\n".join(format_entry(e) for e in entries)


def fts5_trigram_supported() -> bool:
    """True when this SQLite build has FTS5 with the trigram tokenizer."""
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE probe USING fts5(x, tokenize='trigram')")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()


def _check_text(text: object) -> str:
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    stripped = text.strip()
    if not stripped:
        raise ValueError("text must be non-empty")
    if len(stripped) > MAX_TEXT_CHARS:
        raise ValueError(f"text must be at most {MAX_TEXT_CHARS} characters")
    return stripped


def _check_tags(tags: object) -> tuple[str, ...]:
    if tags is None:
        return ()
    if isinstance(tags, str) or not isinstance(tags, Iterable):
        raise ValueError("tags must be a list of strings")
    out: list[str] = []
    for raw in tags:
        if not isinstance(raw, str):
            raise ValueError("tags must be a list of strings")
        tag = raw.strip()
        if not tag or any(ch.isspace() for ch in tag):
            raise ValueError("each tag must be a non-empty string without whitespace")
        if len(tag) > MAX_TAG_CHARS:
            raise ValueError(f"each tag must be at most {MAX_TAG_CHARS} characters")
        if tag not in out:
            out.append(tag)
    if len(out) > MAX_TAGS:
        raise ValueError(f"at most {MAX_TAGS} tags per entry")
    return tuple(out)


def _check_importance(importance: object) -> int:
    if isinstance(importance, bool) or not isinstance(importance, int):
        raise ValueError("importance must be an integer")
    if not MIN_IMPORTANCE <= importance <= MAX_IMPORTANCE:
        raise ValueError(f"importance must be between {MIN_IMPORTANCE} and {MAX_IMPORTANCE}")
    return importance


def _query_terms(query: object) -> tuple[str, ...]:
    if not isinstance(query, str):
        raise ValueError("query must be a string")
    stripped = query.strip()
    if not stripped:
        raise ValueError("query must be non-empty")
    if len(stripped) > MAX_QUERY_CHARS:
        raise ValueError(f"query must be at most {MAX_QUERY_CHARS} characters")
    terms = tuple(dict.fromkeys(stripped.split()))
    if len(terms) > MAX_QUERY_TERMS:
        raise ValueError(f"at most {MAX_QUERY_TERMS} query terms")
    return terms


def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _subject_filter(user_filter: Iterable[str] | None) -> frozenset[str] | None:
    if user_filter is None:
        return None
    return frozenset(check_id(u, "user id") for u in user_filter)


def _visibility_clause(
    viewer: Viewer, user_filter: Iterable[str] | None
) -> tuple[str, list[Any]] | None:
    """SQL predicate for entries the viewer may see, or ``None`` when nothing can be visible.

    Visible iff ``domain`` = viewer domain, ``source_domain`` = viewer domain (so private-derived
    material never reaches a group viewer) and ``subject_user`` is one of the viewer's subjects
    (optionally narrowed by ``user_filter``).
    """
    subjects = viewer.subjects
    narrowed = _subject_filter(user_filter)
    if narrowed is not None:
        subjects = subjects & narrowed
    if not subjects:
        return None
    marks = ", ".join("?" for _ in subjects)
    clause = f"domain = ? AND source_domain = ? AND subject_user IN ({marks})"
    return clause, [viewer.domain, viewer.domain, *sorted(subjects)]


_COLUMNS = "id, subject_user, domain, source_domain, text, tags, importance, created_at, updated_at"


def _row_to_entry(row: tuple[Any, ...]) -> MemoryEntry:
    (eid, subject, domain, source, text, tags, importance, created, updated) = row
    return MemoryEntry(
        id=int(eid),
        subject_user=str(subject),
        domain=str(domain),
        source_domain=str(source),
        text=str(text),
        tags=tuple(str(tags).split()),
        importance=int(importance),
        created_at=float(created),
        updated_at=float(updated),
    )


def _hits(entry: MemoryEntry, terms: tuple[str, ...]) -> int:
    haystack = (entry.text + " " + " ".join(entry.tags)).casefold()
    return sum(haystack.count(term.casefold()) for term in terms)


class MemoryStore:
    """Synchronous SQLite core. Use :class:`cgbot.memory.MemoryBook` from asyncio code."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        clock: Callable[[], float] = time.time,
        use_fts: bool | None = None,
    ) -> None:
        db_path = Path(path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            db_path, timeout=5.0, isolation_level=None, check_same_thread=False
        )
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._fts = self._resolve_fts(use_fts)
            self._init_schema()
        except BaseException:
            self._conn.close()
            raise

    @staticmethod
    def _resolve_fts(use_fts: bool | None) -> bool:
        supported = fts5_trigram_supported()
        if use_fts is None:
            return supported
        if use_fts and not supported:
            raise MemoryStoreError("FTS5 trigram tokenizer is not available in this SQLite build")
        return use_fts

    @property
    def search_backend(self) -> SearchBackend:
        return "fts5" if self._fts else "like"

    def _init_schema(self) -> None:
        with self._transaction() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version "
                "(component TEXT PRIMARY KEY, version INTEGER NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS memory_entries ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "subject_user TEXT NOT NULL, "
                "domain TEXT NOT NULL, "
                "source_domain TEXT NOT NULL, "
                "text TEXT NOT NULL, "
                "tags TEXT NOT NULL, "
                "importance INTEGER NOT NULL CHECK (importance BETWEEN 1 AND 5), "
                "created_at REAL NOT NULL, "
                "updated_at REAL NOT NULL)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS memory_entries_by_domain "
                "ON memory_entries (domain, source_domain, subject_user)"
            )
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (FTS_TABLE,)
            ).fetchone()
            if self._fts:
                conn.execute(
                    f"CREATE VIRTUAL TABLE IF NOT EXISTS {FTS_TABLE} "
                    "USING fts5(text, tags, tokenize='trigram')"
                )
            elif exists is not None:
                raise MemoryStoreError(
                    "memory book has an FTS index; open it with FTS5 support enabled"
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
                raise MemoryStoreError(
                    f"memory schema version {version} is newer than supported {SCHEMA_VERSION}"
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

    # FTS maintenance: always inside the same transaction as the entries row.

    def _fts_put(self, conn: sqlite3.Connection, entry_id: int, text: str, tags: str) -> None:
        if self._fts:
            conn.execute(
                f"INSERT INTO {FTS_TABLE} (rowid, text, tags) VALUES (?, ?, ?)",
                (entry_id, text, tags),
            )

    def _fts_drop(self, conn: sqlite3.Connection, entry_id: int) -> None:
        if self._fts:
            conn.execute(f"DELETE FROM {FTS_TABLE} WHERE rowid = ?", (entry_id,))

    def _select_visible(
        self, conn: sqlite3.Connection, entry_id: int, viewer: Viewer
    ) -> MemoryEntry | None:
        clause = _visibility_clause(viewer, None)
        if clause is None:
            return None
        where, params = clause
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM memory_entries WHERE id = ? AND {where}",
            (entry_id, *params),
        ).fetchone()
        return None if row is None else _row_to_entry(row)

    def add(
        self,
        *,
        subject_user: str,
        domain: str,
        source_domain: str,
        text: str,
        tags: Iterable[str] | None = None,
        importance: int = DEFAULT_IMPORTANCE,
    ) -> MemoryEntry:
        check_id(subject_user, "user id")
        check_domain(domain)
        check_domain(source_domain)
        if domain != source_domain:
            raise CrossDomainWriteError(
                f"refusing cross-domain write: source {source_domain!r} into domain {domain!r}"
            )
        clean_text = _check_text(text)
        clean_tags = _check_tags(tags)
        clean_importance = _check_importance(importance)
        tags_text = " ".join(clean_tags)
        now = self._clock()
        with self._lock, self._transaction() as conn:
            cur = conn.execute(
                "INSERT INTO memory_entries (subject_user, domain, source_domain, text, tags, "
                "importance, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    subject_user,
                    domain,
                    source_domain,
                    clean_text,
                    tags_text,
                    clean_importance,
                    now,
                    now,
                ),
            )
            entry_id = int(cur.lastrowid or 0)
            self._fts_put(conn, entry_id, clean_text, tags_text)
        return MemoryEntry(
            id=entry_id,
            subject_user=subject_user,
            domain=domain,
            source_domain=source_domain,
            text=clean_text,
            tags=clean_tags,
            importance=clean_importance,
            created_at=now,
            updated_at=now,
        )

    def get(self, entry_id: int, viewer: Viewer) -> MemoryEntry | None:
        with self._lock, self._transaction() as conn:
            return self._select_visible(conn, entry_id, viewer)

    def update(
        self,
        entry_id: int,
        *,
        viewer: Viewer,
        text: str | None = None,
        tags: Iterable[str] | None = None,
        importance: int | None = None,
    ) -> MemoryEntry | None:
        """Update text/tags/importance of a visible entry. Domains never change.

        Returns the new entry, or ``None`` when the entry is not visible to ``viewer``.
        """
        new_text = None if text is None else _check_text(text)
        new_tags = None if tags is None else _check_tags(tags)
        new_importance = None if importance is None else _check_importance(importance)
        with self._lock, self._transaction() as conn:
            current = self._select_visible(conn, entry_id, viewer)
            if current is None:
                return None
            final_text = current.text if new_text is None else new_text
            final_tags = current.tags if new_tags is None else new_tags
            final_importance = current.importance if new_importance is None else new_importance
            tags_text = " ".join(final_tags)
            now = self._clock()
            conn.execute(
                "UPDATE memory_entries SET text = ?, tags = ?, importance = ?, updated_at = ? "
                "WHERE id = ?",
                (final_text, tags_text, final_importance, now, entry_id),
            )
            self._fts_drop(conn, entry_id)
            self._fts_put(conn, entry_id, final_text, tags_text)
            return MemoryEntry(
                id=current.id,
                subject_user=current.subject_user,
                domain=current.domain,
                source_domain=current.source_domain,
                text=final_text,
                tags=final_tags,
                importance=final_importance,
                created_at=current.created_at,
                updated_at=now,
            )

    def delete(self, entry_id: int, *, viewer: Viewer) -> bool:
        """Delete a visible entry. ``False`` when it is absent or not visible to ``viewer``."""
        with self._lock, self._transaction() as conn:
            if self._select_visible(conn, entry_id, viewer) is None:
                return False
            conn.execute("DELETE FROM memory_entries WHERE id = ?", (entry_id,))
            self._fts_drop(conn, entry_id)
            return True

    def search(
        self,
        query: str,
        *,
        viewer: Viewer,
        user_filter: Iterable[str] | None,
        limit: int,
    ) -> list[MemoryEntry]:
        terms = _query_terms(query)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        clause = _visibility_clause(viewer, user_filter)
        if clause is None:
            return []
        where, params = clause
        conditions = [where]
        match_params: list[Any] = list(params)
        for term in terms:
            if self._fts and len(term) >= _FTS_MIN_TERM_CHARS:
                conditions.append(
                    f"id IN (SELECT rowid FROM {FTS_TABLE} WHERE {FTS_TABLE} MATCH ?)"
                )
                match_params.append('"' + term.replace('"', '""') + '"')
            else:
                pattern = "%" + _escape_like(term) + "%"
                conditions.append("(text LIKE ? ESCAPE '\\' OR tags LIKE ? ESCAPE '\\')")
                match_params.extend([pattern, pattern])
        sql = f"SELECT {_COLUMNS} FROM memory_entries WHERE {' AND '.join(conditions)}"
        with self._lock:
            rows = self._conn.execute(sql, match_params).fetchall()
        visible = [_row_to_entry(r) for r in rows]
        visible.sort(key=lambda e: (-_hits(e, terms), -e.importance, -e.updated_at, e.id))
        return visible[:limit]

    def list_for_prompt(
        self,
        *,
        viewer: Viewer,
        budget_chars: int,
        user_filter: Iterable[str] | None,
    ) -> list[MemoryEntry]:
        """Visible entries in priority order (importance, recency) that fit ``budget_chars``.

        The length of ``format_entries(result)`` never exceeds ``budget_chars``: each entry costs
        its formatted line, plus one newline separator when it is not the first. Selection stops
        at the first entry that does not fit, so the output is always a priority prefix.
        """
        if isinstance(budget_chars, bool) or not isinstance(budget_chars, int) or budget_chars < 0:
            raise ValueError("budget_chars must be a non-negative integer")
        clause = _visibility_clause(viewer, user_filter)
        if clause is None:
            return []
        where, params = clause
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_COLUMNS} FROM memory_entries WHERE {where} "
                "ORDER BY importance DESC, updated_at DESC, id DESC",
                params,
            ).fetchall()
        out: list[MemoryEntry] = []
        used = 0
        for row in rows:
            entry = _row_to_entry(row)
            cost = len(format_entry(entry)) + (1 if out else 0)
            if used + cost > budget_chars:
                break
            out.append(entry)
            used += cost
        return out

    def close(self) -> None:
        with self._lock:
            self._conn.close()
