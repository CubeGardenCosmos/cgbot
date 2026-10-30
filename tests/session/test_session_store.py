from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

import cgbot
from cgbot.session import AsyncSessionStore, Item, SessionStore, SessionStoreError

SID = "private:10001"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "session.sqlite"


@pytest.fixture
def store(db_path: Path) -> Iterator[SessionStore]:
    s = SessionStore(db_path)
    yield s
    s.close()


def test_append_and_load_keep_seq_order_and_isolate_sessions(store: SessionStore) -> None:
    a = store.append(SID, "user", {"text": "hi"})
    b = store.append("private:10002", "user", {"text": "other user"})
    c = store.append(SID, "assistant", {"text": "hello"})
    d = store.append(SID, "tool_call", {"call_id": "c1", "name": "reply", "arguments": "{}"})
    e = store.append(SID, "tool_result", {"call_id": "c1", "output": "ok"})
    g = store.append("group:777", "user", {"text": "group message"})
    assert a < b < c < d < e < g

    items = store.load(SID)
    assert [i.seq for i in items] == [a, c, d, e]
    assert [i.kind for i in items] == ["user", "assistant", "tool_call", "tool_result"]
    assert items[0] == Item(
        seq=a, session_id=SID, kind="user", payload={"text": "hi"}, ts=items[0].ts
    )
    assert items[0].ts > 0
    assert [i.seq for i in store.load(SID, after_seq=c)] == [d, e]
    assert [i.seq for i in store.load("private:10002")] == [b]
    assert [i.seq for i in store.load("group:777")] == [g]
    assert store.load("private:404") == []


def test_load_active_starts_at_latest_compaction(store: SessionStore) -> None:
    store.append(SID, "user", {"text": "old"})
    store.append(SID, "assistant", {"text": "old reply"})
    first = store.append(SID, "compaction", {"summary": "s1"})
    store.append(SID, "user", {"text": "mid"})
    second = store.append(SID, "compaction", {"summary": "s2"})
    store.append(SID, "user", {"text": "new"})
    assert first < second

    active = store.load_active(SID)
    assert [i.kind for i in active] == ["compaction", "user"]
    assert active[0].seq == second
    assert active[0].payload == {"summary": "s2"}
    assert active[1].payload == {"text": "new"}
    # Pre-compaction records are archived, not deleted.
    assert len(store.load(SID)) == 6


def test_load_active_without_compaction_returns_whole_session(store: SessionStore) -> None:
    seqs = [store.append(SID, "user", {"text": str(i)}) for i in range(3)]
    assert [i.seq for i in store.load_active(SID)] == seqs
    assert store.load_active("private:404") == []


def test_history_survives_reopen_and_seq_continues(db_path: Path) -> None:
    s1 = SessionStore(db_path)
    s1.append(SID, "user", {"text": "one"})
    last = s1.append(SID, "assistant", {"text": "two"})
    s1.close()

    s2 = SessionStore(db_path)
    items = s2.load(SID)
    assert [i.payload for i in items] == [{"text": "one"}, {"text": "two"}]
    nxt = s2.append(SID, "user", {"text": "three"})
    assert nxt > last
    s2.close()


def test_committed_appends_survive_exit_without_close(db_path: Path) -> None:
    src = Path(cgbot.__file__).resolve().parents[1]
    script = (
        "import os, sys\n"
        "from cgbot.session import SessionStore\n"
        "s = SessionStore(sys.argv[1])\n"
        "for i in range(5):\n"
        "    s.append('private:10001', 'user', {'i': i})\n"
        "os._exit(0)\n"
    )
    env = {**os.environ, "PYTHONPATH": str(src)}
    subprocess.run([sys.executable, "-c", script, str(db_path)], env=env, check=True, timeout=60)
    s = SessionStore(db_path)
    assert [i.payload["i"] for i in s.load(SID)] == [0, 1, 2, 3, 4]
    s.close()


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "private",
        "private:",
        "group:",
        "user:1",
        "Private:1",
        "private1",
        " private:1",
        "private: 1",
        "private:1\n",
        "weixin:1",
    ],
)
def test_rejects_invalid_session_ids(store: SessionStore, bad: str) -> None:
    with pytest.raises(ValueError):
        store.append(bad, "user", {})
    with pytest.raises(ValueError):
        store.load(bad)
    with pytest.raises(ValueError):
        store.load_active(bad)
    with pytest.raises(ValueError):
        store.last_activity(bad)


@pytest.mark.parametrize("good", ["private:10001", "group:123456"])
def test_accepts_private_and_group_ids(store: SessionStore, good: str) -> None:
    seq = store.append(good, "user", {"text": "x"})
    assert [i.seq for i in store.load(good)] == [seq]


def test_rejects_invalid_records_without_writing(store: SessionStore) -> None:
    with pytest.raises(ValueError):
        store.append(SID, cast(Any, "bogus"), {})
    with pytest.raises(TypeError):
        store.append(SID, "user", {"x": object()})
    with pytest.raises(ValueError):
        store.append(SID, "user", {"x": float("nan")})
    with pytest.raises(TypeError):
        store.append(SID, "user", cast(Any, ["not", "a", "dict"]))
    with pytest.raises(ValueError):
        store.append(SID, "tool_call", {"name": "reply"})
    with pytest.raises(ValueError):
        store.append(SID, "tool_result", {"call_id": "", "output": "x"})
    with pytest.raises(ValueError):
        store.append(SID, "tool_result", {"call_id": "c1", "output": {"a": 1}})
    with pytest.raises(ValueError):
        store.append(SID, "compaction", {"text": "no summary key"})
    assert store.load(SID) == []


def test_large_tool_output_is_truncated_and_spilled(db_path: Path, tmp_path: Path) -> None:
    spill = tmp_path / "spill"
    s = SessionStore(db_path, spill_dir=spill, tool_output_limit=100)
    small = "ok"
    full = "line\n" * 1000  # 5000 ASCII bytes
    s.append(SID, "tool_result", {"call_id": "c1", "output": small})
    s.append(SID, "tool_result", {"call_id": "c2", "output": full})

    small_item, big = s.load(SID)
    assert small_item.payload == {"call_id": "c1", "output": "ok"}
    assert big.payload["call_id"] == "c2"
    assert big.payload["original_bytes"] == len(full)
    assert len(big.payload["output"].encode("utf-8")) < 100 + 200
    assert big.payload["output"].startswith("line\n")
    assert "truncated" in big.payload["output"]
    spilled = Path(big.payload["spilled_path"])
    assert spilled.parent == spill
    assert spilled.read_text(encoding="utf-8") == full
    s.close()


def test_spill_truncates_on_utf8_boundary(db_path: Path, tmp_path: Path) -> None:
    s = SessionStore(db_path, spill_dir=tmp_path / "spill", tool_output_limit=100)
    full = "好" * 100  # 300 bytes; 100 bytes cuts a 3-byte char
    seq = s.append(SID, "tool_result", {"call_id": "c1", "output": full})
    (item,) = [i for i in s.load(SID) if i.seq == seq]
    assert item.payload["output"].startswith("好" * 33)
    assert Path(item.payload["spilled_path"]).read_text(encoding="utf-8") == full
    s.close()


def test_default_limit_is_64kib_and_spills_into_db_dir(db_path: Path) -> None:
    s = SessionStore(db_path)
    s.append(SID, "tool_result", {"call_id": "ok", "output": "a" * (64 * 1024)})
    s.append(SID, "tool_result", {"call_id": "big", "output": "a" * (64 * 1024 + 1)})
    not_spilled, spilled = s.load(SID)
    assert "spilled_path" not in not_spilled.payload
    path = Path(spilled.payload["spilled_path"])
    assert path.parent == (db_path.parent / "spill").absolute()
    assert path.stat().st_size == 64 * 1024 + 1
    s.close()


async def test_concurrent_async_appends_get_unique_contiguous_seq(db_path: Path) -> None:
    store = AsyncSessionStore(db_path)
    sids = ["private:1", "private:2", "group:9"]
    seqs = await asyncio.gather(*(store.append(sids[i % 3], "user", {"i": i}) for i in range(300)))
    assert sorted(seqs) == list(range(1, 301))

    for idx, sid in enumerate(sids):
        items = await store.load(sid)
        # seq order is commit order; payloads are only guaranteed to form the same set.
        assert [i.seq for i in items] == sorted(i.seq for i in items)
        assert sorted(i.payload["i"] for i in items) == [n for n in range(300) if n % 3 == idx]
    assert await store.list_sessions() == sorted(sids)
    await store.close()


def test_schema_version_row_written_once_and_wal_enabled(db_path: Path) -> None:
    SessionStore(db_path).close()
    SessionStore(db_path).close()
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT component, version FROM schema_version").fetchall()
        mode = conn.execute("PRAGMA journal_mode").fetchone()
    finally:
        conn.close()
    assert rows == [("session", 1)]
    assert mode == ("wal",)


def test_newer_schema_version_is_refused(db_path: Path) -> None:
    SessionStore(db_path).close()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("UPDATE schema_version SET version = 2 WHERE component = 'session'")
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(SessionStoreError):
        SessionStore(db_path)


def test_last_activity_and_list_sessions(store: SessionStore) -> None:
    assert store.last_activity(SID) is None
    assert store.list_sessions() == []
    store.append("private:10002", "user", {"text": "a"})
    store.append("group:5", "user", {"text": "b"})
    store.append(SID, "user", {"text": "c"})
    latest = store.load(SID)[-1].ts
    assert store.last_activity(SID) == latest
    assert latest <= time.time()
    assert store.list_sessions() == ["group:5", SID, "private:10002"]
