from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cgbot.group.buffer import BufferedMessage
from cgbot.group.store import (
    BOT_MESSAGE_RETENTION,
    GroupState,
    GroupStateError,
    GroupStateStore,
)


def msg(i: int) -> BufferedMessage:
    return BufferedMessage(message_id=f"m{i}", user_id="u", ts=float(i), text=f"t{i}")


def test_state_defaults_and_roundtrip_survive_reopen(tmp_path: Path) -> None:
    path = tmp_path / "group_state.sqlite"
    store = GroupStateStore(path)
    assert store.get_state("G1") == GroupState(group_id="G1")
    state = GroupState(
        group_id="G1",
        muted_until=10.5,
        last_proactive_at=3.25,
        last_roll_hour="2026-10-30T09",
        followup_until=7.0,
    )
    store.put_state(state)
    store.close()

    reopened = GroupStateStore(path)
    assert reopened.get_state("G1") == state
    assert reopened.get_state("G2") == GroupState(group_id="G2")
    reopened.close()


def test_put_state_overwrites_previous_row(tmp_path: Path) -> None:
    store = GroupStateStore(tmp_path / "s.sqlite")
    store.put_state(GroupState(group_id="G1", muted_until=1.0))
    store.put_state(GroupState(group_id="G1", muted_until=None, followup_until=2.0))
    assert store.get_state("G1") == GroupState(group_id="G1", followup_until=2.0)
    store.close()


def test_buffer_keeps_newest_capacity_in_order(tmp_path: Path) -> None:
    store = GroupStateStore(tmp_path / "s.sqlite")
    for i in range(35):
        store.append_buffer("G1", msg(i), capacity=30)
    kept = store.buffer("G1")
    assert [m.message_id for m in kept] == [f"m{i}" for i in range(5, 35)]
    assert len(kept) == 30
    store.close()


def test_buffer_is_isolated_per_group_and_clearable(tmp_path: Path) -> None:
    store = GroupStateStore(tmp_path / "s.sqlite")
    store.append_buffer("G1", msg(1), capacity=30)
    store.append_buffer("G2", msg(2), capacity=30)
    assert [m.message_id for m in store.buffer("G1")] == ["m1"]
    store.clear_buffer("G1")
    assert store.buffer("G1") == ()
    assert [m.message_id for m in store.buffer("G2")] == ["m2"]
    store.close()


def test_buffer_rejects_nonpositive_capacity(tmp_path: Path) -> None:
    store = GroupStateStore(tmp_path / "s.sqlite")
    with pytest.raises(ValueError):
        store.append_buffer("G1", msg(1), capacity=0)
    store.close()


def test_bot_message_lookup_and_retention(tmp_path: Path) -> None:
    store = GroupStateStore(tmp_path / "s.sqlite")
    store.record_bot_message("b0", "G1", 0.0)
    assert store.bot_message_group("b0") == "G1"
    assert store.bot_message_group("missing") is None
    for i in range(1, BOT_MESSAGE_RETENTION + 1):
        store.record_bot_message(f"b{i}", "G1", float(i))
    assert store.bot_message_group("b0") is None
    assert store.bot_message_group(f"b{BOT_MESSAGE_RETENTION}") == "G1"
    store.close()


def test_newer_schema_version_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "s.sqlite"
    GroupStateStore(path).close()
    conn = sqlite3.connect(path)
    conn.execute("UPDATE schema_version SET version = 99 WHERE component = 'group_state'")
    conn.commit()
    conn.close()
    with pytest.raises(GroupStateError):
        GroupStateStore(path)
