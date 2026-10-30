"""Application wiring: NapCat ingress -> scope -> identity -> dispatcher -> agent turn -> reply.

Private messages only. The private dispatcher (R2.1/R2.2) decides when each turn starts. Turns
run as background tasks so the NapCat dispatch loop never blocks on a model call or a send.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from cgbot.agent import AgentRunner, builtin_tools
from cgbot.config import Config
from cgbot.dispatch import PrivateDispatcher, PrivateMessage, Timebase
from cgbot.identity import AsyncIdentityStore
from cgbot.model import (
    Channel,
    InboundMessage,
    InputStatus,
    OutboundMessage,
    Segment,
    SendResult,
)
from cgbot.napcat import NapCatServer
from cgbot.provider import ModelProvider, user_message
from cgbot.scheduler import Scheduler, SystemEvent
from cgbot.session import AsyncSessionStore

log = logging.getLogger(__name__)

SPILL_DIR = "spill"

SYSTEM_PROMPT = (
    "你是一个 QQ 私聊助手。你只能通过 reply 工具向用户发送消息;"
    "普通文本输出不会发给任何人。不需要回复时不调用 reply 即为沉默。"
    "回复应简短、自然。"
)

_PLACEHOLDERS = {
    "image": "[图片]",
    "record": "[语音]",
    "video": "[视频]",
    "face": "[表情]",
    "file": "[文件]",
    "forward": "[合并转发]",
}


def render_segments(segments: Sequence[Segment]) -> str:
    """Flatten a message to model-facing text. Non-text media become placeholders."""
    parts: list[str] = []
    for seg in segments:
        if seg.type == "text":
            parts.append(str(seg.data.get("text", "")))
        elif seg.type == "at":
            parts.append(f"@{seg.data.get('qq', '')}")
        elif seg.type == "reply":
            continue
        else:
            parts.append(_PLACEHOLDERS.get(seg.type, "[其他消息]"))
    return "".join(parts).strip()


class App:
    """Owns the components and routes private input through the dispatcher into agent turns."""

    def __init__(
        self, config: Config, provider: ModelProvider, *, timebase: Timebase | None = None
    ) -> None:
        self._config = config
        storage = config.storage
        storage.data_dir.mkdir(parents=True, exist_ok=True)
        # Round 20: one SQLite file per domain; no store opens another module's file.
        self._sessions = AsyncSessionStore(storage.sessions, spill_dir=storage.data_dir / SPILL_DIR)
        self._identity = AsyncIdentityStore(storage.profile)
        self._scheduler = Scheduler(storage.scheduler, self._on_system_event)
        token = config.napcat.token.reveal() if config.napcat.token is not None else None
        self.napcat = NapCatServer(
            config.napcat.host,
            config.napcat.port,
            config.napcat.path,
            token,
            on_message=self.handle_message,
            on_input_status=self.handle_input_status,
        )
        self._runner = AgentRunner(
            provider=provider,
            store=self._sessions,
            tools=builtin_tools(),
            instructions=SYSTEM_PROMPT,
            sender=self._send,
            reasoning_effort=config.provider.reasoning.private,
        )
        self._dispatcher = PrivateDispatcher(self._runner, timebase)
        self._closing = False

    async def start(self) -> None:
        await self.napcat.start()
        await self._scheduler.start()
        log.info("cgbot started: napcat_port=%s", self.napcat.bound_port)

    async def stop(self) -> None:
        self._closing = True
        await self.napcat.stop()
        await self._dispatcher.close()
        await self._scheduler.stop()
        self._scheduler.close()
        await self._sessions.close()
        await self._identity.close()

    async def handle_message(self, message: InboundMessage) -> None:
        if self._closing:
            return
        if message.scope != "private":
            log.debug("group message dropped (M1): message_id=%s", message.message_id)
            return
        if not self._config.scope.allows("private", message.user_id, None):
            log.info("private message out of scope dropped: message_id=%s", message.message_id)
            return
        text = render_segments(message.segments)
        if not text:
            log.info("empty private message ignored: message_id=%s", message.message_id)
            return
        channel = Channel("qq", message.user_id)
        identity = await self._identity.resolve(channel)
        session_id = await self._identity.private_session_id(identity.user_id)
        self._dispatcher.on_message(
            PrivateMessage(
                session_id=session_id,
                user_id=identity.user_id,
                message_id=message.message_id,
                item=user_message(text),
                channel=channel,
                channels=identity.channels,
            )
        )

    async def handle_input_status(self, status: InputStatus) -> None:
        if self._closing:
            return
        if not self._config.scope.allows("private", status.user_id, None):
            return
        identity = await self._identity.resolve(Channel("qq", status.user_id))
        session_id = await self._identity.private_session_id(identity.user_id)
        self._dispatcher.on_typing(session_id)

    async def _send(self, message: OutboundMessage) -> SendResult:
        return await self.napcat.send(message)

    async def _on_system_event(self, event: SystemEvent) -> None:
        # M1 has no system-event routing yet; log identifiers only.
        log.info(
            "system event received (unrouted in M1): event_id=%s kind=%s",
            event.event_id,
            event.kind,
        )
