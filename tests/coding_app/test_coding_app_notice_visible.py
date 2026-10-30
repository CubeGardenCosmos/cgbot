"""Coding/Codex notices reach the model: the notice record carries a clipped, URL-free ``item``.

The check goes through the same path a real turn uses: ``load_active`` then ``to_input_items``.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from cgbot.agent.history import to_input_items
from cgbot.coding.contracts import CodingNotice, StoredFile
from cgbot.coding.notifier import MODEL_TEXT_LIMIT, QqNotifier
from cgbot.model import OutboundMessage, SendResult
from cgbot.session import AsyncSessionStore

SID = "private:1001"


def _notice(**overrides: object) -> CodingNotice:
    fields: dict[str, object] = {
        "kind": "report",
        "user_id": "1001",
        "thread_id": "t1",
        "title": "论文",
        "text": "进展正常",
    }
    fields.update(overrides)
    return CodingNotice(**fields)  # type: ignore[arg-type]


def _stored(name: str = "a.bin") -> StoredFile:
    return StoredFile(
        path=Path("/tmp/a.bin"),
        name=name,
        mime="application/octet-stream",
        size=3,
        kind="file",
        url=f"https://pub.example/bridge/files/tok/{name}",
    )


class StubSender:
    def __init__(self, result: SendResult | None = None, error: Exception | None = None) -> None:
        self.result = result or SendResult(ok=True, message_id="m1")
        self.error = error

    async def __call__(self, message: OutboundMessage) -> SendResult:
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[AsyncSessionStore]:
    s = AsyncSessionStore(tmp_path / "sessions.sqlite", spill_dir=tmp_path / "spill")
    yield s
    await s.close()


async def _model_input(store: AsyncSessionStore) -> str:
    return json.dumps(to_input_items(await store.load_active(SID)), ensure_ascii=False)


async def test_delivered_notice_is_in_model_input(store: AsyncSessionStore) -> None:
    await QqNotifier(StubSender(), store).notify(_notice())

    rendered = await _model_input(store)
    assert "[系统通知] Coding Agent 通知（已送达用户）" in rendered
    assert "进展正常" in rendered


async def test_undelivered_notice_says_so_to_the_model(store: AsyncSessionStore) -> None:
    await QqNotifier(StubSender(SendResult(ok=False, error="rejected")), store).notify(_notice())

    assert "未能送达用户" in await _model_input(store)


async def test_send_exception_is_recorded_undelivered_for_the_model(
    store: AsyncSessionStore,
) -> None:
    await QqNotifier(StubSender(error=RuntimeError("boom")), store).notify(_notice())

    assert "未能送达用户" in await _model_input(store)


async def test_long_body_is_clipped_for_the_model(store: AsyncSessionStore) -> None:
    long_body = "进展" * 2000  # 4000 chars, well over MODEL_TEXT_LIMIT
    await QqNotifier(StubSender(), store).notify(_notice(text=long_body))

    rendered = await _model_input(store)
    assert long_body not in rendered
    assert "…" in rendered
    assert 0 < rendered.count("进展") <= MODEL_TEXT_LIMIT // 2  # two chars per repeat


async def test_attachments_are_named_never_linked(store: AsyncSessionStore) -> None:
    await QqNotifier(StubSender(), store).notify(_notice(files=(_stored("报告.pdf"),)))

    rendered = await _model_input(store)
    assert "附件：报告.pdf" in rendered
    assert "https://" not in rendered


async def test_notice_record_is_metadata_not_a_user_turn(store: AsyncSessionStore) -> None:
    await QqNotifier(StubSender(), store).notify(_notice())

    items = await store.load(SID)
    assert [item.kind for item in items] == ["system_event"]
    assert all(item.kind != "user" for item in items)
