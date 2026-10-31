from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest

from cgbot.agent import ToolContext, ToolSpec
from cgbot.model import Channel, OutboundMessage, SendResult
from cgbot.news import NewsStore, news_fetch_tool, news_subscribe_tool

TZ = ZoneInfo("Asia/Shanghai")
LINK = "https://oss.example/max"


class _Clock:
    def __call__(self) -> datetime:
        return datetime(2026, 11, 1, 10, 0, tzinfo=TZ)


class _Sender:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.messages: list[OutboundMessage] = []

    async def __call__(self, message: OutboundMessage) -> SendResult:
        self.messages.append(message)
        result_ok = self.ok
        return SendResult(
            ok=result_ok, message_id="1" if result_ok else None, error=None if result_ok else "x"
        )


async def _target_of(user_id: str) -> str:
    return f"qq-{user_id}"


def _ctx(session_id: str, user_id: str = "10001", sender: Any = None) -> ToolContext:
    return ToolContext(
        user_id=user_id,
        session_id=session_id,
        source_channel=Channel("qq", user_id),
        source_message_id=None,
        sender=cast(Any, sender if sender is not None else _Sender()),
    )


async def _call(spec: ToolSpec, ctx: ToolContext, args: dict[str, Any]) -> tuple[str, bool]:
    result = await spec.handler(ctx, args)
    return result.output, result.is_error


def _write_issue(output: Path, status: str) -> None:
    day_dir = output / "2026-11-01"
    day_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "issue_id": "2026-W44",
        "status": status,
        "tiers": {"max": {"link": LINK, "label_zh": "完整版", "error": None}},
    }
    (day_dir / "delivery.json").write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def store(tmp_path: Path) -> Iterator[NewsStore]:
    news_store = NewsStore(tmp_path / "news.sqlite")
    yield news_store
    news_store.close()


@pytest.fixture
def output(tmp_path: Path) -> Path:
    return tmp_path / "output"


def test_tool_names_and_enums() -> None:
    # Specs do not touch the store until called, so a placeholder is enough here.
    subscribe = news_subscribe_tool(cast(Any, None), "08:00")
    fetch = news_fetch_tool(Path("unused"), _target_of, _Clock())

    assert subscribe.name == "news_subscribe"
    assert subscribe.parameters_schema["properties"]["action"]["enum"] == [
        "subscribe",
        "unsubscribe",
        "status",
    ]
    assert fetch.name == "news_fetch"
    assert fetch.parameters_schema["properties"]["tier"]["enum"] == [
        "fast",
        "lite",
        "max",
        "all",
    ]


async def test_subscribe_lifecycle_for_own_private_session(store: NewsStore) -> None:
    spec = news_subscribe_tool(store, "08:00")
    ctx = _ctx("private:10001")

    output, is_error = await _call(spec, ctx, {"action": "subscribe"})
    assert not is_error
    assert "08:00" in output
    assert await store.is_subscribed("10001") is True

    output, _ = await _call(spec, ctx, {"action": "subscribe"})
    assert "已经订阅" in output

    output, _ = await _call(spec, ctx, {"action": "status"})
    assert "已订阅" in output

    output, _ = await _call(spec, ctx, {"action": "unsubscribe"})
    assert "已取消" in output
    assert await store.is_subscribed("10001") is False


async def test_group_session_is_refused_and_store_untouched(store: NewsStore) -> None:
    spec = news_subscribe_tool(store, "08:00")

    output, is_error = await _call(spec, _ctx("group:100"), {"action": "subscribe"})

    assert is_error
    assert "私聊" in output
    assert await store.subscribers() == []


async def test_user_comes_from_context_not_from_arguments(store: NewsStore) -> None:
    spec = news_subscribe_tool(store, "08:00")

    _, is_error = await _call(
        spec, _ctx("private:10001"), {"action": "subscribe", "user_id": "99999"}
    )

    assert is_error
    assert await store.subscribers() == []


async def test_private_session_of_another_user_is_refused(store: NewsStore) -> None:
    spec = news_subscribe_tool(store, "08:00")

    _, is_error = await _call(spec, _ctx("private:20002", user_id="10001"), {"action": "subscribe"})

    assert is_error
    assert await store.subscribers() == []


async def test_unknown_or_missing_action_is_rejected(store: NewsStore) -> None:
    spec = news_subscribe_tool(store, "08:00")
    ctx = _ctx("private:10001")

    assert (await _call(spec, ctx, {"action": "delete"}))[1] is True
    assert (await _call(spec, ctx, {}))[1] is True
    assert (await _call(spec, ctx, {"action": "subscribe", "extra": 1}))[1] is True
    assert await store.subscribers() == []


async def test_fetch_refuses_group_session_without_sending(output: Path) -> None:
    _write_issue(output, "complete")
    sender = _Sender()
    spec = news_fetch_tool(output, _target_of, _Clock(), gap_seconds=0)

    _, is_error = await _call(spec, _ctx("group:100", sender=sender), {})

    assert is_error
    assert sender.messages == []


async def test_fetch_sends_to_caller_private_chat(output: Path) -> None:
    _write_issue(output, "complete")
    sender = _Sender()
    spec = news_fetch_tool(output, _target_of, _Clock(), gap_seconds=0)

    output_text, is_error = await _call(spec, _ctx("private:10001", sender=sender), {"tier": "max"})

    assert not is_error
    assert "2026-W44" in output_text
    assert [message.target_id for message in sender.messages] == ["qq-10001"]
    assert LINK in str(sender.messages[0].segments[0].data["text"])


async def test_fetch_when_not_ready_says_so_and_sends_nothing(output: Path) -> None:
    sender = _Sender()
    spec = news_fetch_tool(output, _target_of, _Clock(), gap_seconds=0)

    output_text, is_error = await _call(spec, _ctx("private:10001", sender=sender), {})
    assert not is_error
    assert "还没有生成" in output_text

    _write_issue(output, "running")
    output_text, is_error = await _call(spec, _ctx("private:10001", sender=sender), {})
    assert not is_error
    assert "还没生成完成" in output_text
    assert sender.messages == []


async def test_fetch_rejects_unknown_tier_and_arguments(output: Path) -> None:
    spec = news_fetch_tool(output, _target_of, _Clock(), gap_seconds=0)
    ctx = _ctx("private:10001")

    assert (await _call(spec, ctx, {"tier": "huge"}))[1] is True
    assert (await _call(spec, ctx, {"user_id": "99999"}))[1] is True


async def test_fetch_reports_failed_send_as_error(output: Path) -> None:
    _write_issue(output, "complete")
    spec = news_fetch_tool(output, _target_of, _Clock(), gap_seconds=0)

    output_text, is_error = await _call(
        spec, _ctx("private:10001", sender=_Sender(ok=False)), {"tier": "max"}
    )

    assert is_error
    assert "发送失败" in output_text
