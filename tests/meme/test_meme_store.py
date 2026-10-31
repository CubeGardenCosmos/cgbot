from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from cgbot.meme import MemeEntry, MemeStore, parse_meme
from cgbot.meme.store import (
    MAX_ENTRIES_PER_GROUP,
    MAX_LOOKUP_RESULTS,
    MAX_MEANING_CHARS,
    MAX_TERM_CHARS,
)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[MemeStore]:
    meme_store = MemeStore(tmp_path / "meme.sqlite")
    yield meme_store
    meme_store.close()


def test_parse_meme_splits_on_ascii_and_fullwidth_colon() -> None:
    assert parse_meme("大肥鱼: 群吉祥物") == MemeEntry("大肥鱼", "群吉祥物")
    assert parse_meme("大肥鱼：群吉祥物") == MemeEntry("大肥鱼", "群吉祥物")


def test_parse_meme_without_colon_keeps_whole_text_as_term() -> None:
    assert parse_meme("  躺平  ") == MemeEntry("躺平", "")


def test_parse_meme_empty_head_is_not_split() -> None:
    assert parse_meme(": 只有解释") == MemeEntry(": 只有解释", "")


def test_parse_meme_truncates_to_table_bounds() -> None:
    long_term = parse_meme("甲" * 40 + ": 解释")
    assert long_term == MemeEntry(("甲" * 40 + ": 解释")[:MAX_TERM_CHARS], "")

    long_meaning = parse_meme("鱼: " + "乙" * 300)
    assert long_meaning.term == "鱼"
    assert len(long_meaning.meaning) == MAX_MEANING_CHARS


async def test_upsert_then_lookup_matches_term_and_meaning(store: MemeStore) -> None:
    await store.upsert_many("1", [MemeEntry("大肥鱼", "群吉祥物")], now=1.0)

    assert await store.lookup("大肥", group_id="1") == (MemeEntry("大肥鱼", "群吉祥物"),)
    assert await store.lookup("吉祥", group_id="1") == (MemeEntry("大肥鱼", "群吉祥物"),)


async def test_lookup_is_scoped_to_group_unless_all_groups_requested(store: MemeStore) -> None:
    await store.upsert_many("1", [MemeEntry("鱼", "甲群的解释")], now=1.0)
    await store.upsert_many("2", [MemeEntry("鱼", "乙群的解释")], now=2.0)

    assert await store.lookup("鱼", group_id="1") == (MemeEntry("鱼", "甲群的解释"),)
    assert await store.lookup("鱼", group_id="3") == ()
    everywhere = await store.lookup("鱼", group_id=None)
    assert set(everywhere) == {MemeEntry("鱼", "甲群的解释"), MemeEntry("鱼", "乙群的解释")}


async def test_repeat_without_meaning_keeps_existing_meaning(store: MemeStore) -> None:
    await store.upsert_many("1", [MemeEntry("躺平", "不内卷")], now=1.0)

    await store.upsert_many("1", [MemeEntry("躺平", "")], now=2.0)
    assert await store.lookup("躺平", group_id="1") == (MemeEntry("躺平", "不内卷"),)

    await store.upsert_many("1", [MemeEntry("躺平", "新解释")], now=3.0)
    assert await store.lookup("躺平", group_id="1") == (MemeEntry("躺平", "新解释"),)


async def test_term_identity_and_lookup_are_case_insensitive(store: MemeStore) -> None:
    await store.upsert_many("1", [MemeEntry("YYDS", "永远的神")], now=1.0)
    await store.upsert_many("1", [MemeEntry("yyds", "")], now=2.0)

    hits = await store.lookup("Yyds", group_id="1")
    assert hits == (MemeEntry("yyds", "永远的神"),)


async def test_like_wildcards_in_query_match_literally(store: MemeStore) -> None:
    await store.upsert_many(
        "1",
        [
            MemeEntry("100%", "百分之百"),
            MemeEntry("a_b", "下划线"),
            MemeEntry("c\\d", "反斜杠"),
            MemeEntry("axb", "普通"),
        ],
        now=1.0,
    )

    assert await store.lookup("%", group_id="1") == (MemeEntry("100%", "百分之百"),)
    assert await store.lookup("_", group_id="1") == (MemeEntry("a_b", "下划线"),)
    assert await store.lookup("\\", group_id="1") == (MemeEntry("c\\d", "反斜杠"),)


async def test_exact_term_ranks_before_newer_substring_match(store: MemeStore) -> None:
    await store.upsert_many("1", [MemeEntry("鱼", "")], now=1.0)
    await store.upsert_many("1", [MemeEntry("大鱼", "")], now=5.0)

    hits = await store.lookup("鱼", group_id="1")
    assert [hit.term for hit in hits] == ["鱼", "大鱼"]


async def test_lookup_respects_limit_and_default_cap(store: MemeStore) -> None:
    await store.upsert_many("1", [MemeEntry(f"词{i}", "") for i in range(8)], now=1.0)

    assert len(await store.lookup("词", group_id="1", limit=3)) == 3
    assert len(await store.lookup("词", group_id="1")) == MAX_LOOKUP_RESULTS


async def test_group_keeps_only_newest_entries(store: MemeStore) -> None:
    await store.upsert_many(
        "1", [MemeEntry(f"旧{i}", "") for i in range(MAX_ENTRIES_PER_GROUP)], now=1.0
    )
    await store.upsert_many("1", [MemeEntry("新0", ""), MemeEntry("新1", "")], now=2.0)

    everything = await store.lookup("", group_id="1", limit=MAX_ENTRIES_PER_GROUP + 10)
    terms = {entry.term for entry in everything}
    assert len(terms) == MAX_ENTRIES_PER_GROUP
    assert {"新0", "新1"} <= terms
    assert "旧0" not in terms and "旧1" not in terms
