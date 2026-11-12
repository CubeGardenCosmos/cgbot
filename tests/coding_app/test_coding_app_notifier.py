"""QqNotifier: attachments direct, notice recorded and turn triggered, text fallback."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import cast

import pytest

from cgbot.coding.contracts import CodingNotice, NoticeKind, ReportStatus, StoredFile
from cgbot.coding.notifier import SESSION_EVENT, QqNotifier, fallback_text
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


class RecordingTrigger:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, user_id: str, session_id: str) -> None:
        self.calls.append((user_id, session_id))
        if self.error is not None:
            raise self.error


@pytest.fixture
async def store(tmp_path: Path):
    s = AsyncSessionStore(tmp_path / "sessions.sqlite", spill_dir=tmp_path / "spill")
    yield s
    await s.close()


def _text_of(message: OutboundMessage) -> str:
    return str(message.segments[0].data["text"])


@pytest.mark.parametrize(
    ("kind", "header"),
    [
        ("report", "进度汇报"),
        ("turn_completed", "任务完成"),
        ("turn_failed", "任务失败"),
        ("turn_interrupted", "任务已停止"),
        ("queue_started", "排队指令开始"),
        ("queue_dropped", "排队已清空"),
        ("time_budget", "任务时限提醒"),
    ],
)
def test_fallback_header_names_each_notice_kind(kind: NoticeKind, header: str) -> None:
    assert fallback_text(_notice(kind=kind)).startswith(f"[Coding Agent] {header}")


def test_fallback_title_falls_back_to_thread_id_then_generic_label() -> None:
    assert "：t9" in fallback_text(_notice(title=None, thread_id="t9"))
    assert "：Coding Agent" in fallback_text(_notice(title=None, thread_id=None))


@pytest.mark.parametrize(
    ("status", "label"),
    [
        ("running", "运行中"),
        ("waiting_user_input", "等待你的输入"),
        ("completed", "已完成"),
        ("failed", "失败"),
    ],
)
def test_fallback_status_line_is_localized(status: ReportStatus, label: str) -> None:
    assert f"状态：{label}" in fallback_text(_notice(status=status))


def test_fallback_waiting_status_adds_steer_hint_only_when_waiting() -> None:
    assert "codex_steer" in fallback_text(_notice(status="waiting_user_input"))
    assert "codex_steer" not in fallback_text(_notice(status="completed"))


def test_fallback_never_contains_body_text() -> None:
    assert "进展正常" not in fallback_text(_notice(text="进展正常"))


async def test_text_only_notice_is_recorded_and_triggers_one_turn_without_direct_text(
    store: AsyncSessionStore,
) -> None:
    sender = RecordingSender()
    trigger = RecordingTrigger()
    notifier = QqNotifier(sender, store, trigger)

    await notifier.notify(_notice(kind="turn_completed", status="completed"))

    assert sender.messages == []  # no direct text: the agent summarizes the record instead
    assert trigger.calls == [("1001", "private:1001")]

    items = await store.load("private:1001")
    assert len(items) == 1
    payload = items[0].payload
    assert payload["event"] == SESSION_EVENT
    assert payload["kind"] == "turn_completed"
    assert payload["thread_id"] == "t1"
    assert payload["title"] == "论文"
    assert payload["status"] == "completed"
    assert payload["files"] == 0
    assert payload["files_delivered"] is None
    assert "进展正常" in json.dumps(payload["item"], ensure_ascii=False)


async def test_notice_with_files_sends_only_attachment_segments_then_records_and_triggers(
    store: AsyncSessionStore,
) -> None:
    sender = RecordingSender()
    trigger = RecordingTrigger()
    notifier = QqNotifier(sender, store, trigger)
    notice = _notice(
        files=(
            _stored("image", "p.png"),
            _stored("audio", "v.silk"),
            _stored("video", "m.mp4"),
            _stored("file", "report.pdf"),
        )
    )

    await notifier.notify(notice)

    assert len(sender.messages) == 1
    message = sender.messages[0]
    assert (message.scope, message.target_id) == ("private", "1001")
    assert [seg.type for seg in message.segments] == ["image", "record", "video", "file"]
    assert message.segments[0].data == {"file": "https://pub.example/bridge/files/tok/p.png"}
    assert message.segments[3].data == {
        "file": "https://pub.example/bridge/files/tok/report.pdf",
        "name": "report.pdf",
    }
    assert trigger.calls == [("1001", "private:1001")]

    items = await store.load("private:1001")
    assert len(items) == 1
    assert items[0].payload["files"] == 4
    assert items[0].payload["files_delivered"] is True


async def test_file_send_not_ok_is_recorded_undelivered_and_still_triggers(
    store: AsyncSessionStore, caplog: pytest.LogCaptureFixture
) -> None:
    trigger = RecordingTrigger()
    notifier = QqNotifier(RecordingSender(SendResult(ok=False, error="rejected")), store, trigger)
    with caplog.at_level(logging.WARNING):
        await notifier.notify(_notice(files=(_stored("file"),)))

    items = await store.load("private:1001")
    assert items[0].payload["files_delivered"] is False
    assert trigger.calls == [("1001", "private:1001")]
    assert "not delivered" in caplog.text


async def test_file_send_exception_is_swallowed_and_logged_by_type_only(
    store: AsyncSessionStore, caplog: pytest.LogCaptureFixture
) -> None:
    trigger = RecordingTrigger()
    notifier = QqNotifier(
        RecordingSender(error=RuntimeError("socket secret-detail")), store, trigger
    )
    with caplog.at_level(logging.WARNING):
        await notifier.notify(_notice(files=(_stored("file"),)))

    items = await store.load("private:1001")
    assert items[0].payload["files_delivered"] is False
    assert trigger.calls == [("1001", "private:1001")]
    assert "error=RuntimeError" in caplog.text
    assert "secret-detail" not in caplog.text


async def test_trigger_failure_sends_plain_fallback_without_body_text(
    store: AsyncSessionStore, caplog: pytest.LogCaptureFixture
) -> None:
    sender = RecordingSender()
    notifier = QqNotifier(sender, store, RecordingTrigger(RuntimeError("queue secret-detail")))

    with caplog.at_level(logging.WARNING):
        await notifier.notify(_notice(kind="turn_failed", status="failed", text="进展正常"))

    assert len(sender.messages) == 1
    sent = _text_of(sender.messages[0])
    assert sent.startswith("[Coding Agent] 任务失败：论文")
    assert "状态：失败" in sent
    assert "进展正常" not in sent
    assert "error=RuntimeError" in caplog.text
    assert "secret-detail" not in caplog.text
    assert len(await store.load("private:1001")) == 1  # the record is kept for later turns


async def test_record_failure_skips_trigger_and_sends_fallback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class FailingStore:
        async def append(self, *_args: object) -> None:
            raise OSError("disk full")

    sender = RecordingSender()
    trigger = RecordingTrigger()
    notifier = QqNotifier(sender, cast(AsyncSessionStore, FailingStore()), trigger)
    with caplog.at_level(logging.WARNING):
        await notifier.notify(_notice())

    assert trigger.calls == []
    assert len(sender.messages) == 1
    assert _text_of(sender.messages[0]).startswith("[Coding Agent] 进度汇报：论文")
    assert "history record failed" in caplog.text


async def test_target_of_maps_internal_user_to_qq_account(store: AsyncSessionStore) -> None:
    async def account_of(user_id: str) -> str:
        return f"qq-{user_id}"

    sender = RecordingSender()
    notifier = QqNotifier(
        sender, store, RecordingTrigger(RuntimeError("no turn")), target_of=account_of
    )
    await notifier.notify(_notice(user_id="1001"))

    assert [message.target_id for message in sender.messages] == ["qq-1001"]
