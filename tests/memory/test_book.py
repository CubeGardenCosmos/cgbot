"""MemoryBook storage, search, budgeted listing, persistence and validation (offline)."""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

import cgbot.memory.store as store_module
from cgbot.memory import MemoryBook, MemoryStoreError, Viewer, format_entries
from cgbot.memory.store import fts5_trigram_supported

PRIV = Viewer.private("u1")
GROUP = Viewer.group("g1", ["u1", "u2"])


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture(params=["fts", "like"])
async def book(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[MemoryBook]:
    use_fts = request.param == "fts"
    if use_fts and not fts5_trigram_supported():
        pytest.skip("FTS5 trigram not available in this SQLite build")
    b = MemoryBook(tmp_path / "memory.sqlite", clock=Clock(), use_fts=use_fts)
    assert b.search_backend == ("fts5" if use_fts else "like")
    yield b
    await b.close()


async def _add(book: MemoryBook, text: str, *, importance: int = 3, tags=None) -> int:
    entry = await book.add(
        subject_user="u1",
        domain="private",
        source_domain="private",
        text=text,
        tags=tags,
        importance=importance,
    )
    return entry.id


async def test_cjk_and_ascii_search_matches_expected_entries(book: MemoryBook) -> None:
    coffee = await _add(book, "I love Coffee and 旅行计划 in autumn")
    await _add(book, "prefers tea")
    got = await book.search("旅行计划", viewer=PRIV, limit=10)  # 4 chars: FTS when enabled
    assert [e.id for e in got] == [coffee]
    short = await book.search("旅行", viewer=PRIV, limit=10)  # 2 chars: LIKE in both backends
    assert [e.id for e in short] == [coffee]
    ascii_ci = await book.search("coffee", viewer=PRIV, limit=10)  # case-insensitive ASCII
    assert [e.id for e in ascii_ci] == [coffee]


async def test_like_wildcards_are_literal(book: MemoryBook) -> None:
    literal = await _add(book, "code a_b value")
    await _add(book, "code axb value")
    got = await book.search("a_b", viewer=PRIV, limit=10)
    assert [e.id for e in got] == [literal]
    percent = await book.search("100%", viewer=PRIV, limit=10)
    assert percent == []


async def test_multiple_terms_must_all_match(book: MemoryBook) -> None:
    both = await _add(book, "likes tea and cake")
    await _add(book, "likes coffee")
    got = await book.search("likes cake", viewer=PRIV, limit=10)
    assert [e.id for e in got] == [both]


async def test_ranking_uses_visible_rows_only(book: MemoryBook) -> None:
    # Invisible group material with many hits must not change visible order (U02).
    await book.add_group_profile("g1", "u2", "tea tea tea tea tea")
    weak = await _add(book, "tea once", importance=5)
    strong = await _add(book, "tea tea tea", importance=1)
    got = await book.search("tea", viewer=PRIV, limit=10)
    assert [e.id for e in got] == [strong, weak]


async def test_ranking_ties_break_by_importance_then_recency(book: MemoryBook) -> None:
    low = await _add(book, "tea low", importance=1)
    high = await _add(book, "tea high", importance=5)
    got = await book.search("tea", viewer=PRIV, limit=10)
    assert [e.id for e in got] == [high, low]


async def test_search_limit_and_validation(book: MemoryBook) -> None:
    for i in range(5):
        await _add(book, f"note {i} shared")
    assert len(await book.search("shared", viewer=PRIV, limit=2)) == 2
    with pytest.raises(ValueError):
        await book.search("", viewer=PRIV)
    with pytest.raises(ValueError):
        await book.search("x" * 201, viewer=PRIV)
    with pytest.raises(ValueError):
        await book.search(" ".join(f"t{i}" for i in range(9)), viewer=PRIV)
    with pytest.raises(ValueError):
        await book.search("shared", viewer=PRIV, limit=0)


async def test_update_changes_text_tags_importance_and_search(book: MemoryBook) -> None:
    entry_id = await _add(book, "old phrase alpha", tags=["travel"])
    updated = await book.update(
        entry_id, viewer=PRIV, text="new phrase beta", tags=["food"], importance=5
    )
    assert updated is not None
    assert updated.text == "new phrase beta"
    assert updated.tags == ("food",)
    assert updated.importance == 5
    assert updated.updated_at >= updated.created_at
    assert await book.search("alpha", viewer=PRIV, limit=10) == []
    found = await book.search("beta", viewer=PRIV, limit=10)
    assert [e.id for e in found] == [entry_id]


async def test_tag_text_is_searchable(book: MemoryBook) -> None:
    entry_id = await _add(book, "plain text", tags=["vegetarian"])
    got = await book.search("vegetarian", viewer=PRIV, limit=10)
    assert [e.id for e in got] == [entry_id]


async def test_delete_removes_entry_from_search(book: MemoryBook) -> None:
    entry_id = await _add(book, "temporary fact gamma")
    assert await book.delete(entry_id, viewer=PRIV) is True
    assert await book.search("gamma", viewer=PRIV, limit=10) == []
    assert await book.get(entry_id, viewer=PRIV) is None
    assert await book.delete(entry_id, viewer=PRIV) is False


async def test_list_for_prompt_is_priority_prefix_within_budget(book: MemoryBook) -> None:
    top = await _add(book, "top priority fact", importance=5)
    mid = await _add(book, "middle fact", importance=3)
    low = await _add(book, "low", importance=1)
    listed = await book.list_for_prompt(viewer=PRIV, budget_chars=10_000)
    assert [e.id for e in listed] == [top, mid, low]

    first_line = len(format_entries([listed[0]]))
    one = await book.list_for_prompt(viewer=PRIV, budget_chars=first_line)
    assert [e.id for e in one] == [top]

    text = format_entries(listed[:2])
    two = await book.list_for_prompt(viewer=PRIV, budget_chars=len(text))
    assert [e.id for e in two] == [top, mid]
    assert len(format_entries(two)) <= len(text)


async def test_list_for_prompt_stops_at_first_entry_that_does_not_fit(book: MemoryBook) -> None:
    await _add(book, "x" * 200, importance=5)
    await _add(book, "tiny", importance=1)
    # The long top entry does not fit, so the short lower-priority entry is not taken either.
    assert await book.list_for_prompt(viewer=PRIV, budget_chars=50) == []


async def test_list_for_prompt_budget_zero_and_negative(book: MemoryBook) -> None:
    await _add(book, "anything")
    assert await book.list_for_prompt(viewer=PRIV, budget_chars=0) == []
    with pytest.raises(ValueError):
        await book.list_for_prompt(viewer=PRIV, budget_chars=-1)


async def test_list_for_prompt_respects_group_visibility(book: MemoryBook) -> None:
    await _add(book, "private only")
    profile = await book.add_group_profile("g1", "u2", "group profile fact")
    listed = await book.list_for_prompt(viewer=GROUP, budget_chars=10_000)
    assert [e.id for e in listed] == [profile.id]


async def test_validation_rejects_bad_input(book: MemoryBook) -> None:
    bad_calls = [
        dict(text="   "),
        dict(text="x" * 501),
        dict(text="ok", tags=[str(i) for i in range(9)]),
        dict(text="ok", tags=["has space"]),
        dict(text="ok", tags=["x" * 33]),
        dict(text="ok", tags=[""]),
        dict(text="ok", importance=0),
        dict(text="ok", importance=6),
        dict(text="ok", importance=True),
    ]
    for kwargs in bad_calls:
        with pytest.raises(ValueError):
            await book.add(subject_user="u1", domain="private", source_domain="private", **kwargs)
    with pytest.raises(ValueError):
        await book.add(subject_user="bad id", domain="private", source_domain="private", text="ok")


async def test_tags_are_deduplicated_and_text_stripped(book: MemoryBook) -> None:
    entry = await book.add(
        subject_user="u1",
        domain="private",
        source_domain="private",
        text="  padded text  ",
        tags=["a", "a", "b"],
    )
    assert entry.text == "padded text"
    assert entry.tags == ("a", "b")
    assert entry.importance == 3


async def test_clock_is_used_for_timestamps(tmp_path: Path) -> None:
    clock = Clock()
    b = MemoryBook(tmp_path / "clock.sqlite", clock=clock, use_fts=False)
    try:
        clock.now = 42.0
        entry = await b.add(subject_user="u1", domain="private", source_domain="private", text="t")
        assert entry.created_at == entry.updated_at == 42.0
    finally:
        await b.close()


async def test_entries_persist_across_reopen(tmp_path: Path) -> None:
    path = tmp_path / "persist.sqlite"
    first = MemoryBook(path, clock=Clock())
    entry = await first.add(
        subject_user="u1", domain="private", source_domain="private", text="kept fact", tags=["t"]
    )
    await first.close()

    second = MemoryBook(path, clock=Clock())
    try:
        again = await second.get(entry.id, viewer=PRIV)
        assert again == entry
    finally:
        await second.close()


async def test_schema_version_newer_than_supported_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "future.sqlite"
    await _open_and_close(path)
    conn = sqlite3.connect(path)
    conn.execute("UPDATE schema_version SET version = 99 WHERE component = 'memory'")
    conn.commit()
    conn.close()
    with pytest.raises(MemoryStoreError):
        MemoryBook(path)


async def _open_and_close(path: Path) -> None:
    b = MemoryBook(path, use_fts=False)
    await b.close()


@pytest.mark.skipif(not fts5_trigram_supported(), reason="needs FTS5 trigram")
async def test_fts_database_cannot_open_without_fts(tmp_path: Path) -> None:
    path = tmp_path / "fts.sqlite"
    b = MemoryBook(path, use_fts=True)
    await b.close()
    with pytest.raises(MemoryStoreError):
        MemoryBook(path, use_fts=False)


async def test_forcing_fts_on_unsupported_build_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store_module, "fts5_trigram_supported", lambda: False)
    with pytest.raises(MemoryStoreError):
        MemoryBook(tmp_path / "nofts.sqlite", use_fts=True)


async def test_auto_backend_falls_back_to_like_when_fts_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store_module, "fts5_trigram_supported", lambda: False)
    b = MemoryBook(tmp_path / "auto.sqlite")
    try:
        assert b.search_backend == "like"
        entry = await b.add(
            subject_user="u1", domain="private", source_domain="private", text="旅行计划"
        )
        assert [e.id for e in await b.search("旅行计划", viewer=PRIV, limit=5)] == [entry.id]
    finally:
        await b.close()
