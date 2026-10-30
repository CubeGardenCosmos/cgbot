"""Offline unit tests for the identity store (R1.3)."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from cgbot.identity import (
    AsyncIdentityStore,
    ChannelConflictError,
    IdentityStore,
    IdentityStoreError,
    UnknownUserError,
)
from cgbot.identity.store import SCHEMA_COMPONENT, SCHEMA_VERSION
from cgbot.model import Channel

QQ_A = Channel("qq", "10001")
QQ_B = Channel("qq", "10002")


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "cgbot.sqlite"


@pytest.fixture
def store(db_path: Path) -> Iterator[IdentityStore]:
    s = IdentityStore(db_path)
    yield s
    s.close()


def test_resolve_is_idempotent_for_same_qq(store: IdentityStore) -> None:
    first = store.resolve(QQ_A)
    second = store.resolve(QQ_A)
    assert first == second
    assert first.channels == (QQ_A,)
    assert store.private_session_id(first.user_id) == store.private_session_id(second.user_id)


def test_two_qq_accounts_get_different_identities(store: IdentityStore) -> None:
    a = store.resolve(QQ_A)
    b = store.resolve(QQ_B)
    assert a.user_id != b.user_id
    assert a.channels == (QQ_A,)
    assert b.channels == (QQ_B,)
    assert store.private_session_id(a.user_id) != store.private_session_id(b.user_id)


def test_identity_is_not_the_platform_account(store: IdentityStore) -> None:
    identity = store.resolve(QQ_A)
    assert identity.user_id != QQ_A.account


def test_persists_across_reopen(db_path: Path) -> None:
    s1 = IdentityStore(db_path)
    created = s1.resolve(QQ_A)
    s1.add_channel(created.user_id, Channel("qq", "10003"))
    s1.close()

    s2 = IdentityStore(db_path)
    try:
        again = s2.resolve(QQ_A)
        assert again.user_id == created.user_id
        assert again.channels == (QQ_A, Channel("qq", "10003"))
        assert s2.identity_for_user(created.user_id) == again
        assert s2.resolve(QQ_B).user_id != created.user_id
    finally:
        s2.close()


def test_new_identity_ids_do_not_reuse_after_reopen(db_path: Path) -> None:
    s1 = IdentityStore(db_path)
    first = s1.resolve(QQ_A).user_id
    s1.close()
    s2 = IdentityStore(db_path)
    try:
        second = s2.resolve(QQ_B).user_id
    finally:
        s2.close()
    assert first != second


def test_identity_for_user(store: IdentityStore) -> None:
    identity = store.resolve(QQ_A)
    assert store.identity_for_user(identity.user_id) == identity
    assert store.identity_for_user("u999") is None


def test_add_channel_binds_second_channel_in_order(store: IdentityStore) -> None:
    identity = store.resolve(QQ_A)
    extra = Channel("qq", "10003")
    store.add_channel(identity.user_id, extra)
    got = store.identity_for_user(identity.user_id)
    assert got is not None
    assert got.channels == (QQ_A, extra)
    assert store.resolve(extra).user_id == identity.user_id


def test_add_channel_same_identity_is_idempotent(store: IdentityStore) -> None:
    identity = store.resolve(QQ_A)
    store.add_channel(identity.user_id, QQ_A)
    got = store.identity_for_user(identity.user_id)
    assert got is not None
    assert got.channels == (QQ_A,)


def test_add_channel_conflict_with_other_identity(store: IdentityStore) -> None:
    a = store.resolve(QQ_A)
    b = store.resolve(QQ_B)
    with pytest.raises(ChannelConflictError):
        store.add_channel(b.user_id, QQ_A)
    # Nothing changed for either identity.
    got_a = store.identity_for_user(a.user_id)
    got_b = store.identity_for_user(b.user_id)
    assert got_a is not None and got_a.channels == (QQ_A,)
    assert got_b is not None and got_b.channels == (QQ_B,)


def test_add_channel_unknown_user(store: IdentityStore) -> None:
    with pytest.raises(UnknownUserError):
        store.add_channel("u404", QQ_A)


def test_unique_constraint_enforced_by_database(db_path: Path, store: IdentityStore) -> None:
    store.resolve(QQ_A)
    conn = sqlite3.connect(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO identity_channels (kind, account, user_id, created_at) "
                "VALUES ('qq', '10001', 'u-other', 0)"
            )
    finally:
        conn.close()


def test_preferred_channel_is_first_bound(store: IdentityStore) -> None:
    identity = store.resolve(QQ_B)
    assert store.preferred_channel(identity.user_id) == QQ_B
    store.add_channel(identity.user_id, QQ_A)
    assert store.preferred_channel(identity.user_id) == QQ_B


def test_preferred_channel_unknown_user(store: IdentityStore) -> None:
    with pytest.raises(UnknownUserError):
        store.preferred_channel("u404")


def test_private_session_id_format(store: IdentityStore) -> None:
    identity = store.resolve(QQ_A)
    session_id = store.private_session_id(identity.user_id)
    assert session_id == f"private:{identity.user_id}"
    assert session_id.startswith("private:")
    assert not session_id.startswith("group:")
    # Derived from the internal identity, not from the QQ account.
    assert QQ_A.account not in session_id


def test_private_session_id_one_per_user_across_channels(store: IdentityStore) -> None:
    identity = store.resolve(QQ_A)
    store.add_channel(identity.user_id, QQ_B)
    assert store.resolve(QQ_B).user_id == identity.user_id
    assert store.private_session_id(identity.user_id) == f"private:{identity.user_id}"


@pytest.mark.parametrize("bad", ["", "u 1", "private:u1", "../x", "group:1"])
def test_private_session_id_rejects_invalid_user_id(store: IdentityStore, bad: str) -> None:
    with pytest.raises(ValueError):
        store.private_session_id(bad)


@pytest.mark.parametrize(
    "channel",
    [
        Channel("qq", ""),
        Channel("qq", "12a"),
        Channel("qq", " 123"),
        Channel("wx", "123"),  # type: ignore[arg-type]  # deliberately unknown kind
    ],
)
def test_resolve_rejects_invalid_channel(store: IdentityStore, channel: Channel) -> None:
    with pytest.raises(ValueError):
        store.resolve(channel)


def test_concurrent_resolve_same_channel_yields_one_identity(store: IdentityStore) -> None:
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda _: store.resolve(QQ_A), range(64)))
    assert len({r.user_id for r in results}) == 1
    assert all(r.channels == (QQ_A,) for r in results)


def test_concurrent_resolve_across_connections_yields_one_identity(db_path: Path) -> None:
    stores = [IdentityStore(db_path) for _ in range(4)]
    try:

        def work(i: int) -> str:
            return stores[i % len(stores)].resolve(QQ_A).user_id

        with ThreadPoolExecutor(max_workers=16) as pool:
            ids = set(pool.map(work, range(64)))
        assert len(ids) == 1
    finally:
        for s in stores:
            s.close()


def test_schema_version_row_in_shared_db_file(db_path: Path, store: IdentityStore) -> None:
    store.resolve(QQ_A)
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT version FROM schema_version WHERE component = ?", (SCHEMA_COMPONENT,)
        ).fetchone()
    finally:
        conn.close()
    assert row == (SCHEMA_VERSION,)


def test_newer_schema_version_is_rejected(db_path: Path) -> None:
    IdentityStore(db_path).close()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE schema_version SET version = ? WHERE component = ?",
            (SCHEMA_VERSION + 1, SCHEMA_COMPONENT),
        )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(IdentityStoreError):
        IdentityStore(db_path)


async def test_async_wrapper_matches_sync_behavior(db_path: Path) -> None:
    store = AsyncIdentityStore(db_path)
    try:
        results = await asyncio.gather(*(store.resolve(QQ_A) for _ in range(20)))
        first = results[0]
        assert all(r == first for r in results)
        other = await store.resolve(QQ_B)
        assert other.user_id != first.user_id
        assert await store.identity_for_user(first.user_id) == first
        assert await store.identity_for_user("u404") is None
        assert await store.private_session_id(first.user_id) == f"private:{first.user_id}"
        extra = Channel("qq", "10009")
        await store.add_channel(first.user_id, extra)
        assert await store.preferred_channel(first.user_id) == QQ_A
        with pytest.raises(ChannelConflictError):
            await store.add_channel(other.user_id, QQ_A)
        with pytest.raises(UnknownUserError):
            await store.preferred_channel("u404")
    finally:
        await store.close()
