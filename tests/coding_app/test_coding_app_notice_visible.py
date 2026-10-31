"""model_text: clipped body, attachment names only, and a request for a persona summary."""

from __future__ import annotations

from pathlib import Path

from cgbot.coding.contracts import CodingNotice, StoredFile
from cgbot.coding.notifier import MODEL_TEXT_LIMIT, NOTICE_PREFIX, model_text


def _notice(**overrides: object) -> CodingNotice:
    fields: dict[str, object] = {
        "kind": "turn_completed",
        "user_id": "1001",
        "thread_id": "t1",
        "title": "论文",
        "text": "完成了",
        "status": "completed",
    }
    fields.update(overrides)
    return CodingNotice(**fields)  # type: ignore[arg-type]


def _stored(name: str) -> StoredFile:
    return StoredFile(
        path=Path("/tmp/x"),
        name=name,
        mime="application/octet-stream",
        size=1,
        kind="file",
        url=f"https://pub.example/bridge/files/tok/{name}",
    )


def test_model_text_is_prefixed_instruction_style_and_asks_for_persona_summary() -> None:
    text = model_text(_notice(), files_delivered=None)
    assert text.startswith(NOTICE_PREFIX)
    assert "任务完成：论文" in text
    assert "状态：已完成" in text
    assert "完成了" in text


def test_model_text_clips_body_to_limit() -> None:
    body = "进展" * 2000  # longer than MODEL_TEXT_LIMIT, so it must be clipped
    text = model_text(_notice(text=body), files_delivered=None)
    assert body not in text
    assert "…" in text
    assert len(text) < MODEL_TEXT_LIMIT + 400


def test_model_text_names_attachments_with_delivery_state_and_never_urls() -> None:
    notice = _notice(files=(_stored("a.bin"), _stored("b.pdf")))
    delivered = model_text(notice, files_delivered=True)
    undelivered = model_text(notice, files_delivered=False)
    assert "a.bin、b.pdf" in delivered
    assert "已直接发给用户" in delivered
    assert "未能送达用户" in undelivered
    assert "https://" not in delivered
    assert "https://" not in undelivered


def test_model_text_has_no_attachment_line_without_files() -> None:
    assert "附件" not in model_text(_notice(), files_delivered=None)


def test_model_text_adds_waiting_guidance_without_tool_name() -> None:
    text = model_text(_notice(status="waiting_user_input", text=""), files_delivered=None)
    assert "等待用户输入" in text
    assert "codex_steer" not in text
