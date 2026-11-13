"""QQ delivery of Coding Agent notices (M5 R5.4).

``QqNotifier`` implements ``contracts.CodingNotifier``. Each notice takes three steps:

1. Attachments (image, voice, video, file) are sent directly to the owning user as one private
   message without text. The notice text is never sent directly.
2. The notice is recorded as a ``system_event`` in that user's private session. Its ``item`` is the
   model-visible notice (``model_text``), which asks the agent to summarize it in persona voice
   through its reply tool.
3. A turn trigger queues one private turn over that record. The record is the turn's input; the
   trigger adds no input item.

If the record or the trigger fails, a plain-text fallback (header and status, no body) is sent
directly so a finished task is never silent.

Delivery never raises: the manager and the bridge both call ``notify`` from background paths. Logs
carry kind and exception type only.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Awaitable, Callable

from cgbot.coding.contracts import CodingNotice, FileKind, NoticeKind, ReportStatus, StoredFile
from cgbot.memory.domain import PRIVATE_SESSION_PREFIX
from cgbot.model import OutboundMessage, Segment, SendResult
from cgbot.provider import user_message
from cgbot.session import AsyncSessionStore

log = logging.getLogger(__name__)

Sender = Callable[[OutboundMessage], Awaitable[SendResult]]
# (internal user id, private session id) -> queues one turn over the stored history.
TurnTrigger = Callable[[str, str], Awaitable[None]]

SESSION_EVENT = "coding_notice"
MODEL_TEXT_LIMIT = 1500
NOTICE_PREFIX = "[系统通知] "

_HEADERS: dict[NoticeKind, str] = {
    "report": "进度汇报",
    "turn_completed": "任务完成",
    "turn_failed": "任务失败",
    "turn_interrupted": "任务已停止",
    "queue_started": "排队指令开始",
    "queue_dropped": "排队已清空",
    "time_budget": "任务时限提醒",
}
_STATUSES: dict[ReportStatus, str] = {
    "running": "运行中",
    "waiting_user_input": "等待你的输入",
    "completed": "已完成",
    "failed": "失败",
}
_WAITING_HINT = "（该任务在等待你的输入：用 codex_steer 回复它）"
_WAITING_MODEL_HINT = "该任务在等待用户输入:告诉用户它在等他回复,说明回一句话它就能继续。"
_SUMMARIZE_HINT = "请用一两句人设口吻向用户简短总结这条结果,不要复述原文,不讲技术细节,不列清单。"
_FILE_SEGMENTS: dict[FileKind, str] = {
    "image": "image",
    "audio": "file",
    "video": "video",
    "file": "file",
}


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _file_segment(stored: StoredFile) -> Segment:
    """音频仅在显式 send_as=voice 时作为语音(record)发出;其余按文件发出并保留文件名。"""
    if stored.kind == "audio" and stored.send_as == "voice":
        return Segment("record", {"file": stored.url})
    segment = _FILE_SEGMENTS[stored.kind]
    data: dict[str, object] = {"file": stored.url}
    if segment == "file":
        data["name"] = stored.name
    return Segment(segment, data)


def _header(notice: CodingNotice) -> str:
    title = notice.title or notice.thread_id or "Coding Agent"
    return f"{_HEADERS[notice.kind]}：{title}"


def _status_line(notice: CodingNotice) -> str | None:
    return None if notice.status is None else f"状态：{_STATUSES[notice.status]}"


def fallback_text(notice: CodingNotice) -> str:
    """Plain text sent when the notice could not be recorded or turned into a turn: no body."""
    lines = [f"[Coding Agent] {_header(notice)}"]
    status = _status_line(notice)
    if status is not None:
        lines.append(status)
    if notice.status == "waiting_user_input":
        lines.append(_WAITING_HINT)
    return "\n".join(lines)


def model_text(notice: CodingNotice, files_delivered: bool | None) -> str:
    """Model-visible form of a notice: a system instruction with the clipped body.

    Attachment names only, never URLs. ``files_delivered`` is None when the notice has no files.
    """
    clipped = dataclasses.replace(notice, text=_clip(notice.text, MODEL_TEXT_LIMIT))
    lines = [f"{NOTICE_PREFIX}Coding Agent 通知。{_header(clipped)}"]
    status = _status_line(clipped)
    if status is not None:
        lines.append(status)
    if clipped.text:
        lines.append(clipped.text)
    if clipped.status == "waiting_user_input":
        lines.append(_WAITING_MODEL_HINT)
    if notice.files:
        state = "已直接发给用户" if files_delivered else "未能送达用户"
        lines.append(f"附件({state}):" + "、".join(stored.name for stored in notice.files))
    lines.append(_SUMMARIZE_HINT)
    return "\n".join(lines)


class QqNotifier:
    """Sends attachments to the owning user, records the notice, and triggers its summary turn."""

    def __init__(
        self,
        send: Sender,
        sessions: AsyncSessionStore,
        start_turn: TurnTrigger,
        target_of: Callable[[str], Awaitable[str]] | None = None,
    ) -> None:
        self._send = send
        self._sessions = sessions
        self._start_turn = start_turn
        self._target_of = target_of  # internal user id -> QQ account; default: identical

    async def notify(self, notice: CodingNotice) -> None:
        files_delivered = await self._send_files(notice)
        recorded = await self._record(notice, files_delivered)
        triggered = recorded and await self._trigger(notice)
        if not triggered:
            await self._send_fallback(notice)

    async def _send_to_user(self, user_id: str, segments: tuple[Segment, ...]) -> SendResult:
        target = user_id
        if self._target_of is not None:
            target = await self._target_of(user_id)
        message = OutboundMessage(scope="private", target_id=target, segments=segments)
        return await self._send(message)

    async def _send_files(self, notice: CodingNotice) -> bool | None:
        """Attachments only, in one message without text. None when the notice has no files."""
        if not notice.files:
            return None
        try:
            result = await self._send_to_user(
                notice.user_id, tuple(_file_segment(stored) for stored in notice.files)
            )
            if result.ok:
                return True
            log.warning("coding notice files not delivered: kind=%s", notice.kind)
        except Exception as exc:
            log.warning(
                "coding notice files delivery failed: kind=%s error=%s",
                notice.kind,
                type(exc).__name__,
            )
        return False

    async def _record(self, notice: CodingNotice, files_delivered: bool | None) -> bool:
        payload: dict[str, object] = {
            "event": SESSION_EVENT,
            "kind": notice.kind,
            "thread_id": notice.thread_id,
            "title": notice.title,
            "status": notice.status,
            "files": len(notice.files),
            "files_delivered": files_delivered,
            "item": user_message(model_text(notice, files_delivered)),
        }
        try:
            await self._sessions.append(
                f"{PRIVATE_SESSION_PREFIX}{notice.user_id}", "system_event", payload
            )
        except Exception as exc:
            log.warning("coding notice history record failed: error=%s", type(exc).__name__)
            return False
        return True

    async def _trigger(self, notice: CodingNotice) -> bool:
        session_id = f"{PRIVATE_SESSION_PREFIX}{notice.user_id}"
        try:
            await self._start_turn(notice.user_id, session_id)
        except Exception as exc:
            log.warning(
                "coding notice turn not started: kind=%s error=%s",
                notice.kind,
                type(exc).__name__,
            )
            return False
        return True

    async def _send_fallback(self, notice: CodingNotice) -> None:
        try:
            result = await self._send_to_user(
                notice.user_id, (Segment("text", {"text": fallback_text(notice)}),)
            )
            if not result.ok:
                log.warning("coding notice fallback not delivered: kind=%s", notice.kind)
        except Exception as exc:
            log.warning(
                "coding notice fallback failed: kind=%s error=%s",
                notice.kind,
                type(exc).__name__,
            )
