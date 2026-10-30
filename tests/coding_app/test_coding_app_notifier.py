"""QqNotifier: rendering, delivery failure isolation, history record without body text."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import cast

import pytest

from cgbot.coding.contracts import CodingNotice, NoticeKind, ReportStatus, StoredFile
from cgbot.coding.notifier import SESSION_EVENT, TEXT_LIMIT, QqNotifier, render_notice
from cgbot.model import OutboundMessage, SendResult
from cgbot.session import AsyncSessionStore


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


def _stored(kind: str, name: str = "a.bin") -> StoredFile:
    return StoredFile(
        path=Path("/tmp/a.bin"),
        name=name,
        mime="application/octet-stream",
        size=3,
        kind=kind,  # type: ignore[arg-type]
        url=f"https://pub.example/bridge/files/tok/{name}",
    )


class RecordingSender:
    def __init__(self, result: SendResult | None = None, error: Exception | None = None) -> None:
        self.result = result or SendResult(ok=True, message_id="m1")
        self.error = error
        self.messages: list[OutboundMessage] = []

    async def __call__(self, message: OutboundMessage) -> SendResult:
        self.messages.append(message)
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture
async def store(tmp_path: Path):
    s = AsyncSessionStore(tmp_path / "sessions.sqlite", spill_dir=tmp_path / "spill")
    yield s
    await s.close()


@pytest.mark.parametrize(
    ("kind", "header"),
    [
        ("report", "进度汇报：论文"),
        ("turn_completed", "任务完成：论文"),
        ("turn_failed", "任务失败：论文"),
        ("turn_interrupted", "任务已停止：论文"),
        ("queue_started", "排队指令开始：论文"),
        ("queue_dropped", "排队已清空：论文"),
    ],
)
def test_header_names_each_notice_kind(kind: NoticeKind, header: str) -> None:
    segments = render_notice(_notice(kind=kind))
    assert segments[0].type == "text"
    assert str(segments[0].data["text"]).startswith(f"[Coding Agent] {header}")


def test_title_falls_back_to_thread_id_then_generic_label() -> None:
    by_thread = render_notice(_notice(title=None, thread_id="t9"))
    assert "：t9" in str(by_thread[0].data["text"])
    generic = render_notice(_notice(title=None, thread_id=None))
    assert "：Coding Agent" in str(generic[0].data["text"])


@pytest.mark.parametrize(
    ("status", "label"),
    [
        ("running", "状态：运行中"),
        ("waiting_user_input", "状态：等待你的输入"),
        ("completed", "状态：已完成"),
        ("failed", "状态：失败"),
    ],
)
def test_status_line_is_localized(status: ReportStatus, label: str) -> None:
    text = str(render_notice(_notice(status=status))[0].data["text"])
    assert label in text


def test_waiting_status_adds_steer_hint_only_when_waiting() -> None:
    waiting = str(render_notice(_notice(status="waiting_user_input"))[0].data["text"])
    assert "codex_steer" in waiting
    completed = str(render_notice(_notice(status="completed"))[0].data["text"])
    assert "codex_steer" not in completed


def test_body_text_is_clipped_to_limit() -> None:
    text = str(render_notice(_notice(text="长" * (TEXT_LIMIT + 200)))[0].data["text"])
    assert "…" in text
    assert len(text) < TEXT_LIMIT + 200


def test_file_segments_map_kinds_and_name_only_plain_files() -> None:
    notice = _notice(
        files=(
            _stored("image", "p.png"),
            _stored("audio", "v.silk"),
            _stored("video", "m.mp4"),
            _stored("file", "report.pdf"),
        )
    )
    segments = render_notice(notice)
    assert [seg.type for seg in segments] == ["text", "image", "record", "video", "file"]
    assert segments[1].data == {"file": "https://pub.example/bridge/files/tok/p.png"}
    assert segments[2].data == {"file": "https://pub.example/bridge/files/tok/v.silk"}
    assert segments[4].data == {
        "file": "https://pub.example/bridge/files/tok/report.pdf",
        "name": "report.pdf",
    }


async def test_notify_sends_private_message_and_records_delivery(store: AsyncSessionStore) -> None:
    sender = RecordingSender()
    notifier = QqNotifier(sender, store)
    await notifier.notify(_notice(kind="turn_completed", status="completed"))

    assert len(sender.messages) == 1
    message = sender.messages[0]
    assert (message.scope, message.target_id) == ("private", "1001")

    items = await store.load("private:1001")
    assert len(items) == 1
    payload = items[0].payload
    assert payload["event"] == SESSION_EVENT
    assert payload["kind"] == "turn_completed"
    assert payload["thread_id"] == "t1"
    assert payload["title"] == "论文"
    assert payload["status"] == "completed"
    assert payload["files"] == 0
    assert payload["delivered"] is True


async def test_history_record_carries_clipped_model_item_without_urls(
    store: AsyncSessionStore,
) -> None:
    notifier = QqNotifier(RecordingSender(), store)
    long_body = "进展" * 2000  # longer than MODEL_TEXT_LIMIT, so it must be clipped
    await notifier.notify(_notice(text=long_body, files=(_stored("file"),)))
    items = await store.load("private:1001")
    payload = items[0].payload
    assert "text" not in payload  # top-level metadata only; the body lives in the model item
    rendered = json.dumps(payload["item"], ensure_ascii=False)
    assert long_body not in rendered
    assert "…" in rendered
    assert "[系统通知]" in rendered
    assert "a.bin" in rendered
    assert "https://" not in rendered


async def test_send_result_not_ok_is_recorded_undelivered(
    store: AsyncSessionStore, caplog: pytest.LogCaptureFixture
) -> None:
    notifier = QqNotifier(RecordingSender(SendResult(ok=False, error="rejected")), store)
    with caplog.at_level(logging.WARNING):
        await notifier.notify(_notice())
    items = await store.load("private:1001")
    assert items[0].payload["delivered"] is False
    assert "not delivered" in caplog.text


async def test_send_exception_is_swallowed_and_logged_by_type_only(
    store: AsyncSessionStore, caplog: pytest.LogCaptureFixture
) -> None:
    notifier = QqNotifier(RecordingSender(error=RuntimeError("socket secret-detail")), store)
    with caplog.at_level(logging.WARNING):
        await notifier.notify(_notice())
    items = await store.load("private:1001")
    assert items[0].payload["delivered"] is False
    assert "error=RuntimeError" in caplog.text
    assert "secret-detail" not in caplog.text


async def test_history_failure_does_not_raise(caplog: pytest.LogCaptureFixture) -> None:
    class FailingStore:
        async def append(self, *_args: object) -> None:
            raise OSError("disk full")

    sender = RecordingSender()
    notifier = QqNotifier(sender, cast(AsyncSessionStore, FailingStore()))
    with caplog.at_level(logging.WARNING):
        await notifier.notify(_notice())
    assert len(sender.messages) == 1
    assert "history record failed" in caplog.text
