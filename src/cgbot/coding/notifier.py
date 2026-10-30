"""QQ delivery of Coding Agent notices (M5 R5.4).

``QqNotifier`` implements ``contracts.CodingNotifier``: each notice is sent as one private QQ
message to the owning user, then recorded as a ``system_event`` in that user's session history.
The record carries the model-visible form of the notice (``item``, a user-role input item built
by ``model_text``), so the next turn of that session sees it. Recording never starts a turn.

Delivery never raises: the manager and the bridge both call ``notify`` from background paths, and
a failed send must not affect turn bookkeeping. Logs carry kind and exception type only.
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

TEXT_LIMIT = 4000
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
}
_STATUSES: dict[ReportStatus, str] = {
    "running": "运行中",
    "waiting_user_input": "等待你的输入",
    "completed": "已完成",
    "failed": "失败",
}
_WAITING_HINT = "（该任务在等待你的输入：用 codex_steer 回复它）"
_FILE_SEGMENTS: dict[FileKind, str] = {
    "image": "image",
    "audio": "record",
    "video": "video",
    "file": "file",
}


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _file_segment(stored: StoredFile) -> Segment:
    data: dict[str, object] = {"file": stored.url}
    if stored.kind == "file":
        data["name"] = stored.name
    return Segment(_FILE_SEGMENTS[stored.kind], data)


def render_notice(notice: CodingNotice) -> tuple[Segment, ...]:
    """Segments for one notice: a text header/body, then one segment per attached file."""
    title = notice.title or notice.thread_id or "Coding Agent"
    lines = [f"[Coding Agent] {_HEADERS[notice.kind]}：{title}"]
    if notice.status is not None:
        lines.append(f"状态：{_STATUSES[notice.status]}")
    if notice.text:
        lines.append(_clip(notice.text, TEXT_LIMIT))
    if notice.status == "waiting_user_input":
        lines.append(_WAITING_HINT)
    segments = [Segment("text", {"text": "\n".join(lines)})]
    segments.extend(_file_segment(stored) for stored in notice.files)
    return tuple(segments)


def model_text(notice: CodingNotice, delivered: bool) -> str:
    """Model-visible form of a notice: clipped QQ text plus attachment names (never URLs)."""
    clipped = dataclasses.replace(notice, text=_clip(notice.text, MODEL_TEXT_LIMIT))
    state = "已送达用户" if delivered else "未能送达用户"
    body = str(render_notice(clipped)[0].data["text"])
    lines = [f"{NOTICE_PREFIX}Coding Agent 通知（{state}）", body]
    if notice.files:
        lines.append("附件：" + "、".join(stored.name for stored in notice.files))
    return "\n".join(lines)


class QqNotifier:
    """Sends notices to the owning user's private chat and records them in session history."""

    def __init__(
        self,
        send: Sender,
        sessions: AsyncSessionStore,
        target_of: Callable[[str], Awaitable[str]] | None = None,
    ) -> None:
        self._send = send
        self._sessions = sessions
        self._target_of = target_of  # internal user id -> QQ account; default: identical

    async def notify(self, notice: CodingNotice) -> None:
        delivered = False
        try:
            target = notice.user_id
            if self._target_of is not None:
                target = await self._target_of(notice.user_id)
            result = await self._send(
                OutboundMessage(
                    scope="private",
                    target_id=target,
                    segments=render_notice(notice),
                )
            )
            delivered = result.ok
            if not delivered:
                log.warning("coding notice not delivered: kind=%s", notice.kind)
        except Exception as exc:
            log.warning(
                "coding notice delivery failed: kind=%s error=%s",
                notice.kind,
                type(exc).__name__,
            )
        await self._record(notice, delivered)

    async def _record(self, notice: CodingNotice, delivered: bool) -> None:
        payload: dict[str, object] = {
            "event": SESSION_EVENT,
            "kind": notice.kind,
            "thread_id": notice.thread_id,
            "title": notice.title,
            "status": notice.status,
            "files": len(notice.files),
            "delivered": delivered,
            "item": user_message(model_text(notice, delivered)),
        }
        try:
            await self._sessions.append(
                f"{PRIVATE_SESSION_PREFIX}{notice.user_id}", "system_event", payload
            )
        except Exception as exc:
            log.warning("coding notice history record failed: error=%s", type(exc).__name__)
