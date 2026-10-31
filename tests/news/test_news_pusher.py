from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from cgbot.model import OutboundMessage, SendResult
from cgbot.news import NewsPusher, NewsStore
from cgbot.news.delivery import PARTIAL_NOTE

TZ = ZoneInfo("Asia/Shanghai")
DAY = "2026-11-01"
LINK = "https://oss.example/max"


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, 1, hour, minute, tzinfo=TZ)


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class _FakeSend:
    def __init__(
        self, *, raise_for: frozenset[str] = frozenset(), refuse_for: frozenset[str] = frozenset()
    ) -> None:
        self.raise_for = raise_for
        self.refuse_for = refuse_for
        self.messages: list[OutboundMessage] = []

    async def __call__(self, message: OutboundMessage) -> SendResult:
        if message.target_id in self.raise_for:
            raise RuntimeError("napcat unreachable")
        if message.target_id in self.refuse_for:
            return SendResult(ok=False, error="refused")
        self.messages.append(message)
        return SendResult(ok=True, message_id=str(len(self.messages)))

    def targets(self) -> list[str]:
        return [message.target_id for message in self.messages]


async def _target_of(user_id: str) -> str:
    return f"qq-{user_id}"


def _write_issue(output: Path, status: str) -> None:
    day_dir = output / DAY
    day_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "issue_id": "2026-W44",
        "status": status,
        "tiers": {"max": {"link": LINK, "label_zh": "完整版", "error": None}},
    }
    (day_dir / "delivery.json").write_text(json.dumps(payload), encoding="utf-8")


def _pusher(
    store: NewsStore, output: Path, send: _FakeSend, clock: _Clock, *, give_up_hour: int = 12
) -> NewsPusher:
    return NewsPusher(
        store=store,
        output_dir=output,
        send=send,
        target_of=_target_of,
        push_time=time(8, 0),
        give_up_hour=give_up_hour,
        clock=clock,
        poll_seconds=0,
        gap_seconds=0,
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[NewsStore]:
    news_store = NewsStore(tmp_path / "news.sqlite")
    yield news_store
    news_store.close()


@pytest.fixture
def output(tmp_path: Path) -> Path:
    return tmp_path / "output"


async def test_nothing_is_sent_before_push_time(store: NewsStore, output: Path) -> None:
    await store.subscribe("10001")
    _write_issue(output, "complete")
    send = _FakeSend()
    clock = _Clock(_at(7, 59))

    assert await _pusher(store, output, send, clock).tick() == 0
    assert send.messages == []


async def test_complete_issue_is_pushed_once_per_day(store: NewsStore, output: Path) -> None:
    await store.subscribe("10001")
    _write_issue(output, "complete")
    send = _FakeSend()
    clock = _Clock(_at(8, 0))
    pusher = _pusher(store, output, send, clock)

    assert await pusher.tick() == 1
    pushed = len(send.messages)
    clock.now = _at(9, 30)
    assert await pusher.tick() == 0
    assert len(send.messages) == pushed
    assert set(send.targets()) == {"qq-10001"}
    assert LINK in str(send.messages[0].segments[0].data["text"])


async def test_incomplete_issue_waits_then_pushes_partial_at_give_up(
    store: NewsStore, output: Path
) -> None:
    await store.subscribe("10001")
    _write_issue(output, "running")
    send = _FakeSend()
    clock = _Clock(_at(11, 59))
    pusher = _pusher(store, output, send, clock)

    assert await pusher.tick() == 0
    assert send.messages == []

    clock.now = _at(12, 0)
    assert await pusher.tick() == 1
    assert PARTIAL_NOTE in str(send.messages[-1].segments[0].data["text"])


async def test_no_issue_at_give_up_sends_apology(store: NewsStore, output: Path) -> None:
    await store.subscribe("10001")
    send = _FakeSend()
    clock = _Clock(_at(12, 0))

    assert await _pusher(store, output, send, clock).tick() == 1
    assert "没能按时出来" in str(send.messages[0].segments[0].data["text"])


async def test_one_failing_subscriber_does_not_block_others(store: NewsStore, output: Path) -> None:
    await store.subscribe("a", now=1.0)
    await store.subscribe("b", now=2.0)
    _write_issue(output, "complete")
    send = _FakeSend(raise_for=frozenset({"qq-a"}))
    clock = _Clock(_at(8, 5))
    pusher = _pusher(store, output, send, clock)

    assert await pusher.tick() == 2
    assert set(send.targets()) == {"qq-b"}
    assert await store.was_sent("a", "2026-W44") is True

    clock.now = _at(9, 0)
    assert await pusher.tick() == 0  # a failed attempt is not retried on every poll


async def test_refused_send_is_logged_and_not_retried(store: NewsStore, output: Path) -> None:
    await store.subscribe("a")
    _write_issue(output, "complete")
    send = _FakeSend(refuse_for=frozenset({"qq-a"}))
    clock = _Clock(_at(8, 5))
    pusher = _pusher(store, output, send, clock)

    assert await pusher.tick() == 1
    assert await pusher.tick() == 0
    assert send.messages == []


async def test_late_subscriber_gets_an_already_pushed_issue_once(
    store: NewsStore, output: Path
) -> None:
    await store.subscribe("a")
    _write_issue(output, "complete")
    send = _FakeSend()
    clock = _Clock(_at(8, 5))
    pusher = _pusher(store, output, send, clock)
    await pusher.tick()
    first_round = send.targets().count("qq-a")

    await store.subscribe("b")
    clock.now = _at(10, 0)
    assert await pusher.tick() == 1
    targets = send.targets()
    assert targets.count("qq-a") == first_round
    assert targets.count("qq-b") == first_round


async def test_restart_does_not_resend_the_same_issue(tmp_path: Path, output: Path) -> None:
    path = tmp_path / "news.sqlite"
    _write_issue(output, "complete")
    send = _FakeSend()
    clock = _Clock(_at(8, 5))

    first = NewsStore(path)
    await first.subscribe("10001")
    assert await _pusher(first, output, send, clock).tick() == 1
    pushed = len(send.messages)
    first.close()

    second = NewsStore(path)
    try:
        clock.now = _at(15, 0)
        assert await _pusher(second, output, send, clock).tick() == 0
    finally:
        second.close()
    assert len(send.messages) == pushed
