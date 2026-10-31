from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from cgbot.news import NewsStore


@pytest.fixture
def store(tmp_path: Path) -> Iterator[NewsStore]:
    news_store = NewsStore(tmp_path / "news.sqlite")
    yield news_store
    news_store.close()


async def test_subscribe_is_idempotent(store: NewsStore) -> None:
    assert await store.subscribe("10001", now=1.0) is True
    assert await store.subscribe("10001", now=2.0) is False
    assert await store.subscribers() == ["10001"]


async def test_unsubscribe_reports_whether_a_row_was_removed(store: NewsStore) -> None:
    await store.subscribe("10001")
    assert await store.unsubscribe("10001") is True
    assert await store.unsubscribe("10001") is False
    assert await store.is_subscribed("10001") is False


async def test_subscribers_listed_in_subscription_order(store: NewsStore) -> None:
    await store.subscribe("b", now=2.0)
    await store.subscribe("a", now=1.0)
    assert await store.subscribers() == ["a", "b"]


async def test_mark_sent_is_per_user_and_per_issue(store: NewsStore) -> None:
    await store.mark_sent("10001", "2026-11-01", now=1.0)
    await store.mark_sent("10001", "2026-11-01", now=2.0)  # idempotent

    assert await store.was_sent("10001", "2026-11-01") is True
    assert await store.was_sent("10001", "2026-11-02") is False
    assert await store.was_sent("10002", "2026-11-01") is False


async def test_sent_ledger_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "news.sqlite"
    first = NewsStore(path)
    await first.subscribe("10001")
    await first.mark_sent("10001", "2026-11-01")
    first.close()

    second = NewsStore(path)
    try:
        assert await second.subscribers() == ["10001"]
        assert await second.was_sent("10001", "2026-11-01") is True
    finally:
        second.close()
