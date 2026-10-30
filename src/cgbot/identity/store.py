"""Identity and channel bindings in SQLite (R1.3).

Model (DESIGN.md §4):

- An ``Identity`` is the internal owner of a user's conversation history. It has an
  internal ``user_id`` (``u1``, ``u2``, ...) that never equals a platform account.
- A ``Channel`` (入口) is a platform account reaching that identity, e.g.
  ``Channel("qq", "123456")``. ``(kind, account)`` is globally unique: one account
  belongs to at most one identity.
- Each identity has exactly one private master session, ``private:<user_id>``. It is
  derived from ``user_id``, never from a channel, so channels cannot create extra
  private sessions. Group sessions (``group:<id>``) are not identity-owned and are
  never returned by this module.

Concurrency: one connection per store guarded by a ``threading.Lock``. Every write
runs in ``BEGIN IMMEDIATE`` so get-or-create is atomic across threads and across
processes sharing the DB file; the ``UNIQUE(kind, account)`` constraint is the backstop.

Storage (round 20): this store owns ``profile.sqlite`` under the storage data dir — user
profile/config data only. Chat records (sessions) and other modules use their own files; this
schema never shares a database file with them. It keeps a ``schema_version`` row for component
``identity``.
"""

from __future__ import annotations

import asyncio
import os
import re
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast, get_args

from cgbot.model import Channel, ChannelKind

SCHEMA_COMPONENT = "identity"
SCHEMA_VERSION = 1

_QQ_ACCOUNT = re.compile(r"[0-9]+")
_USER_ID = re.compile(r"[A-Za-z0-9_-]+")
_KINDS: frozenset[str] = frozenset(get_args(ChannelKind))


class IdentityError(Exception):
    """Base class for identity-store errors."""


class IdentityStoreError(IdentityError):
    """Store-level failure: schema newer than this code, or an invalid stored row."""


class ChannelConflictError(IdentityError):
    """The channel is already bound to a different identity."""


class UnknownUserError(IdentityError):
    """No identity with the given internal user_id exists."""


@dataclass(frozen=True, slots=True)
class Identity:
    """An internal user with its channels, in binding order (first = primary)."""

    user_id: str
    channels: tuple[Channel, ...]


def _check_channel(channel: Channel) -> None:
    if not isinstance(channel, Channel):
        raise TypeError("channel must be a Channel")
    if channel.kind not in _KINDS:
        raise ValueError(f"unknown channel kind: {channel.kind!r}")
    if not isinstance(channel.account, str):
        raise TypeError("channel account must be a str")
    if channel.kind == "qq" and not _QQ_ACCOUNT.fullmatch(channel.account):
        raise ValueError("qq channel account must be a decimal QQ number string")


def _check_user_id(user_id: str) -> None:
    if not isinstance(user_id, str) or not _USER_ID.fullmatch(user_id):
        raise ValueError("invalid user id")


def _as_kind(value: Any) -> ChannelKind:
    text = str(value)
    if text not in _KINDS:
        raise IdentityStoreError(f"corrupt channel kind in store: {text!r}")
    return cast(ChannelKind, text)


class IdentityStore:
    """SQLite-backed identity registry with get-or-create channel resolution."""

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

    def _init_schema(self) -> None:
        with self._transaction() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version "
                "(component TEXT PRIMARY KEY, version INTEGER NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS identities ("
                "seq INTEGER PRIMARY KEY, "
                "user_id TEXT NOT NULL UNIQUE, "
                "created_at REAL NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS identity_channels ("
                "seq INTEGER PRIMARY KEY AUTOINCREMENT, "
                "kind TEXT NOT NULL, "
                "account TEXT NOT NULL, "
                "user_id TEXT NOT NULL REFERENCES identities (user_id), "
                "created_at REAL NOT NULL, "
                "UNIQUE (kind, account))"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS identity_channels_by_user "
                "ON identity_channels (user_id, seq)"
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
                raise IdentityStoreError(
                    f"identity schema version {version} is newer than supported {SCHEMA_VERSION}"
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

    def resolve(self, channel: Channel) -> Identity:
        """Return the identity bound to ``channel``, creating identity + binding if new."""
        _check_channel(channel)
        with self._lock, self._transaction() as conn:
            row = conn.execute(
                "SELECT user_id FROM identity_channels WHERE kind = ? AND account = ?",
                (channel.kind, channel.account),
            ).fetchone()
            if row is None:
                user_id = self._new_user(conn)
                conn.execute(
                    "INSERT INTO identity_channels (kind, account, user_id, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (channel.kind, channel.account, user_id, time.time()),
                )
            else:
                user_id = str(row[0])
            return self._load(conn, user_id)

    def _new_user(self, conn: sqlite3.Connection) -> str:
        # Called only inside BEGIN IMMEDIATE, so MAX+1 is race-free. Identities are
        # never deleted, so the sequence has no gaps that matter.
        (next_seq,) = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM identities").fetchone()
        user_id = f"u{next_seq}"
        conn.execute(
            "INSERT INTO identities (seq, user_id, created_at) VALUES (?, ?, ?)",
            (next_seq, user_id, time.time()),
        )
        return user_id

    def _load(self, conn: sqlite3.Connection, user_id: str) -> Identity:
        rows = conn.execute(
            "SELECT kind, account FROM identity_channels WHERE user_id = ? ORDER BY seq",
            (user_id,),
        ).fetchall()
        channels = tuple(Channel(kind=_as_kind(k), account=str(a)) for k, a in rows)
        return Identity(user_id=user_id, channels=channels)

    def identity_for_user(self, user_id: str) -> Identity | None:
        """The identity with this internal user_id, or None if it does not exist."""
        _check_user_id(user_id)
        with self._lock:
            exists = self._conn.execute(
                "SELECT 1 FROM identities WHERE user_id = ?", (user_id,)
            ).fetchone()
            if exists is None:
                return None
            return self._load(self._conn, user_id)

    def private_session_id(self, user_id: str) -> str:
        """``private:<user_id>``: the identity's only private master session."""
        _check_user_id(user_id)
        return f"private:{user_id}"

    def add_channel(self, user_id: str, channel: Channel) -> None:
        """Bind another channel to an existing identity (reserved for multi-channel).

        Idempotent when the channel is already bound to the same identity. Raises
        ``ChannelConflictError`` if it belongs to a different identity, and
        ``UnknownUserError`` if the identity does not exist.
        """
        _check_user_id(user_id)
        _check_channel(channel)
        with self._lock, self._transaction() as conn:
            known = conn.execute(
                "SELECT 1 FROM identities WHERE user_id = ?", (user_id,)
            ).fetchone()
            if known is None:
                raise UnknownUserError(f"unknown user id: {user_id}")
            row = conn.execute(
                "SELECT user_id FROM identity_channels WHERE kind = ? AND account = ?",
                (channel.kind, channel.account),
            ).fetchone()
            if row is not None:
                if str(row[0]) == user_id:
                    return
                raise ChannelConflictError(
                    f"channel {channel.kind}:{channel.account} is bound to another identity"
                )
            conn.execute(
                "INSERT INTO identity_channels (kind, account, user_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (channel.kind, channel.account, user_id, time.time()),
            )

    def preferred_channel(self, user_id: str) -> Channel:
        """Primary channel: the first bound one. Usage-based choice is M6, not here."""
        _check_user_id(user_id)
        with self._lock:
            row = self._conn.execute(
                "SELECT kind, account FROM identity_channels WHERE user_id = ? "
                "ORDER BY seq LIMIT 1",
                (user_id,),
            ).fetchone()
        if row is None:
            raise UnknownUserError(f"no channel bound for user id: {user_id}")
        return Channel(kind=_as_kind(row[0]), account=str(row[1]))

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class AsyncIdentityStore:
    """asyncio facade over ``IdentityStore``: each call runs in a worker thread."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._store = IdentityStore(path)

    async def resolve(self, channel: Channel) -> Identity:
        return await asyncio.to_thread(self._store.resolve, channel)

    async def identity_for_user(self, user_id: str) -> Identity | None:
        return await asyncio.to_thread(self._store.identity_for_user, user_id)

    async def private_session_id(self, user_id: str) -> str:
        return await asyncio.to_thread(self._store.private_session_id, user_id)

    async def add_channel(self, user_id: str, channel: Channel) -> None:
        await asyncio.to_thread(self._store.add_channel, user_id, channel)

    async def preferred_channel(self, user_id: str) -> Channel:
        return await asyncio.to_thread(self._store.preferred_channel, user_id)

    async def close(self) -> None:
        await asyncio.to_thread(self._store.close)
