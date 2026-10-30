from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cgbot.coding.queue_store import QueueStore, QueueStoreError


def test_enqueue_assigns_increasing_seq_and_lists_oldest_first(tmp_path: Path) -> None:
    store = QueueStore(tmp_path / "coding.sqlite")
    first = store.enqueue("alice", "t1", "p1", None, 1.0)
    second = store.enqueue("bob", "t2", "p2", "T", 2.0)
    third = store.enqueue("alice", "t1", "p3", None, 3.0)
    assert first < second < third
    assert [(j.seq, j.user_id, j.prompt) for j in store.pending()] == [
        (first, "alice", "p1"),
        (second, "bob", "p2"),
        (third, "alice", "p3"),
    ]
    store.close()


def test_start_job_moves_pending_to_running_and_finish_clears(tmp_path: Path) -> None:
    store = QueueStore(tmp_path / "coding.sqlite")
    seq = store.enqueue("alice", "t1", "p1", None, 1.0)
    store.start_job(seq, "alice", "t1", "turn-9", 2.0)
    assert store.pending() == []
    [running] = store.running_turns()
    assert (running.user_id, running.thread_id, running.turn_id) == ("alice", "t1", "turn-9")
    store.finish_turn("alice", "t1")
    assert store.running_turns() == []
    store.close()


def test_delete_jobs_removes_only_listed_rows(tmp_path: Path) -> None:
    store = QueueStore(tmp_path / "coding.sqlite")
    keep = store.enqueue("alice", "t1", "keep", None, 1.0)
    drop = store.enqueue("alice", "t1", "drop", None, 2.0)
    store.delete_jobs([])
    store.delete_jobs([drop])
    assert [j.seq for j in store.pending()] == [keep]
    store.close()


def test_state_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "coding.sqlite"
    store = QueueStore(path)
    queued = store.enqueue("alice", "t1", "waiting", "title", 1.0)
    started = store.enqueue("alice", "t2", "running", None, 2.0)
    store.start_job(started, "alice", "t2", "turn-1", 3.0)
    store.close()

    reopened = QueueStore(path)
    assert [(j.seq, j.prompt, j.title) for j in reopened.pending()] == [
        (queued, "waiting", "title")
    ]
    assert [(r.thread_id, r.turn_id) for r in reopened.running_turns()] == [("t2", "turn-1")]
    reopened.close()


def test_close_is_idempotent_and_rejects_further_calls(tmp_path: Path) -> None:
    store = QueueStore(tmp_path / "coding.sqlite")
    store.close()
    store.close()
    with pytest.raises(QueueStoreError):
        store.pending()


def test_newer_schema_version_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "coding.sqlite"
    QueueStore(path).close()
    conn = sqlite3.connect(path)
    conn.execute("UPDATE schema_version SET version = 99 WHERE component = 'coding'")
    conn.commit()
    conn.close()
    with pytest.raises(QueueStoreError):
        QueueStore(path)
