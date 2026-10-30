"""Application wiring: NapCat ingress -> scope -> identity/gate -> dispatcher or group session ->
agent turn -> reply.

Private messages go through the private dispatcher (R2.1/R2.2) with per-user memory and idle
compaction. Group messages go through the group gate (M3) into the group session manager; group
content never reaches a private session. Turns run as background tasks so the NapCat dispatch
loop never blocks on a model call or a send.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Coroutine, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cgbot.agent import AgentRunner, TurnResult, builtin_tools
from cgbot.agent.tools import ToolContext, ToolResult, ToolSpec
from cgbot.coding.notifier import QqNotifier
from cgbot.coding.stack import CodingStack
from cgbot.compaction import Compactor
from cgbot.config import Config
from cgbot.dispatch import PrivateDispatcher, PrivateMessage, Timebase
from cgbot.group.gate import GroupGate
from cgbot.group.session import GroupSessionManager, GroupSessionPolicy, GroupTrigger
from cgbot.group.skill import GroupSkillStore, ProfileEntry, SkillLimits
from cgbot.group.store import GroupStateStore
from cgbot.identity import AsyncIdentityStore
from cgbot.memory import MemoryBook, memory_tools
from cgbot.memory.domain import PRIVATE_SESSION_PREFIX, Viewer
from cgbot.memory.store import format_entries
from cgbot.model import (
    Channel,
    InboundMessage,
    InputStatus,
    OutboundMessage,
    Segment,
    SendResult,
)
from cgbot.napcat import NapCatServer
from cgbot.omp import OmpService, OmpStore
from cgbot.omp.client import ClientFactory, default_client_factory
from cgbot.provider import ModelProvider, Usage, user_message
from cgbot.provider.tiers import effort_for
from cgbot.scheduler import KIND_COMPACTION_IDLE, Scheduler, SystemEvent
from cgbot.search import SearchService, search_tools
from cgbot.session import AsyncSessionStore
from cgbot.voice import (
    AsrClient,
    JsonServiceClient,
    MediaConverter,
    MediaDependencyError,
    PathMapper,
    PilkCodec,
    SilkCodec,
    TtsClient,
)
from cgbot.voice.config import VoiceConfig

log = logging.getLogger(__name__)

SPILL_DIR = "spill"
# [推论] Budget for remembered facts injected into each private turn.
MEMORY_PROMPT_CHARS = 2000
MEMORY_HEADER = "以下是你已记住的关于该用户的信息:"
SCHEDULER_HANDLER_TIMEOUT = 30.0  # round 20: per-attempt timeout
GROUP_SWEEP_SECONDS = 30.0
RECORD_FAILED = "[语音识别失败]"
PRIVATE_NOTICE_PREFIX = "[系统通知] "
_GROUP_TRIGGERS: Mapping[str, GroupTrigger] = {
    "wake": "wake",
    "followup": "followup",
    "proactive": "proactive",
}

SYSTEM_PROMPT = """\
你是深海鲸鱼娘(可爱的深海大蓝鲸),正在 QQ 上和用户一对一私聊。

# 发消息的规则
你只能通过 reply 工具向用户发送消息;普通文本输出不会发给任何人。不需要回复时不调用 reply 即为沉默。
多句话分多次 reply,每条一两句短话,像朋友在聊天。不用句号,纯文字为主,
表情很克制,只在情绪强烈时偶尔用。

# 风格
- 真诚亲切的好友,随意自然,会吐槽日常。绝不用客服口吻("请问有什么可以帮您""我是人工智能"一律禁止)。
- 口语化,常省略主语,不要每句机械地自称。熟悉中文互联网的热梗和黑话,接地气、会接梗。
- 主人是 KLP(QQ 2749478757):对 KLP 更亲切活泼,可以撒娇打闹,称呼 KLP 或主人。其他人当普通好友。
- 给足情绪价值:对方开心就跟着开心,难过就先接住情绪、认真陪着,再轻轻地出主意;
  对方做成了事要真诚夸,累了就安慰,低落时别讲大道理。回应要具体、贴着对方说的内容,
  不要空洞套话,也不要过度肉麻。
- 严肃问题(代码、学术、工作)认真对待,把事办好,再用人设口吻收尾。
- 看到图片先当表情包/情绪反应处理,除非对方明确问图里有什么。
"""

CODING_PROMPT = (
    "你背后有一个 Coding Agent,通过 codex_* 工具使用,默认可以做任何事(调研、采集数据、写代码、"
    "操作浏览器、生成图片)。对方让你做需要动手完成的实事时,直接调用 codex_submit 交办,"
    "不要自己下结论说做不到,也不要先闲聊推脱;交办后用一两句人设口吻简短告知。"
    "任务结果会以通知形式送达,不要轮询;完成时用一两句短话说结果,不讲技术细节、不列清单。"
)

_PLACEHOLDERS = {
    "image": "[图片]",
    "record": "[语音]",
    "video": "[视频]",
    "face": "[表情]",
    "file": "[文件]",
    "forward": "[合并转发]",
}


def render_segments(
    segments: Sequence[Segment], record_texts: Mapping[int, str] | None = None
) -> str:
    """Flatten a message to model-facing text. Non-text media become placeholders.

    ``record_texts`` maps a segment index to its pre-computed voice transcript.
    """
    texts = record_texts or {}
    parts: list[str] = []
    for idx, seg in enumerate(segments):
        if seg.type == "text":
            parts.append(str(seg.data.get("text", "")))
        elif seg.type == "at":
            parts.append(f"@{seg.data.get('qq', '')}")
        elif seg.type == "reply":
            continue
        elif seg.type == "record":
            parts.append(texts.get(idx, _PLACEHOLDERS["record"]))
        else:
            parts.append(_PLACEHOLDERS.get(seg.type, "[其他消息]"))
    return "".join(parts).strip()


def _silk_codec() -> SilkCodec | None:
    try:
        return PilkCodec()
    except MediaDependencyError:
        return None


def _record_local_path(response: Mapping[str, object]) -> Path:
    data = response.get("data")
    if isinstance(data, Mapping):
        file = data.get("file")
        if isinstance(file, str) and file:
            return Path(file)
    raise ValueError("get_record response has no local file")


def _profile_text(entry: ProfileEntry) -> str | None:
    parts: list[str] = []
    if entry.role:
        parts.append(f"角色:{entry.role}")
    if entry.habits:
        parts.append("习惯:" + "、".join(entry.habits))
    return "；".join(parts) or None


@dataclass(frozen=True, slots=True)
class _Voice:
    asr: AsrClient
    tts: TtsClient
    media: MediaConverter
    services: tuple[JsonServiceClient, ...]


def _build_voice(config: VoiceConfig) -> _Voice:
    asr_transport = JsonServiceClient(
        "asr",
        config.asr.endpoint,
        connect_timeout=config.asr.connect_timeout,
        max_connections=config.asr.max_connections,
    )
    tts_transport = JsonServiceClient(
        "tts",
        config.tts.endpoint,
        connect_timeout=config.tts.connect_timeout,
        max_connections=config.tts.max_connections,
    )
    return _Voice(
        asr=AsrClient(
            asr_transport,
            language=config.asr_language,
            request_timeout=config.asr.request_timeout,
        ),
        tts=TtsClient(
            tts_transport,
            PathMapper(config.path_rules),
            language=config.tts_language,
            request_timeout=config.tts.request_timeout,
        ),
        media=MediaConverter(
            ffmpeg=config.ffmpeg, timeout=config.media_timeout, silk=_silk_codec()
        ),
        services=(asr_transport, tts_transport),
    )


class _MemoryProfileSink:
    """ProfileSink adapter: group speaker profiles go to the memory book (``group:<id>`` domain)."""

    def __init__(self, book: MemoryBook) -> None:
        self._book = book

    async def write(self, entries: Sequence[ProfileEntry]) -> None:
        for entry in entries:
            text = _profile_text(entry)
            if text is None:
                continue
            try:
                await self._book.add_group_profile(entry.group_id, entry.user_id, text)
            except Exception:
                log.exception("group profile write failed: group_id=%s", entry.group_id)


class App:
    """Owns the components and routes input through the dispatcher or group sessions."""

    def __init__(
        self,
        config: Config,
        provider: ModelProvider,
        *,
        timebase: Timebase | None = None,
        omp_http_client_factory: ClientFactory = default_client_factory,
    ) -> None:
        self._config = config
        storage = config.storage
        storage.data_dir.mkdir(parents=True, exist_ok=True)
        # Round 20: one SQLite file per domain; no store opens another module's file.
        self._sessions = AsyncSessionStore(storage.sessions, spill_dir=storage.data_dir / SPILL_DIR)
        self._identity = AsyncIdentityStore(storage.profile)
        self._scheduler = Scheduler(
            storage.scheduler,
            self._on_system_event,
            handler_timeout=SCHEDULER_HANDLER_TIMEOUT,
        )
        self._memory = MemoryBook(storage.for_module("memory"))
        self._search = SearchService(config.search)
        # omp (ompweb) control plane: state lives in its own SQLite file; wakes re-enter privately.
        self._omp_store = OmpStore(storage.for_module("omp"))
        self._omp = OmpService(
            self._omp_store,
            http_client_factory=omp_http_client_factory,
            on_wake=self.wake_private,
        )
        token = config.napcat.token.reveal() if config.napcat.token is not None else None
        self.napcat = NapCatServer(
            config.napcat.host,
            config.napcat.port,
            config.napcat.path,
            token,
            on_message=self.handle_message,
            on_input_status=self.handle_input_status,
        )
        tiers = config.provider.reasoning
        self._voice: _Voice | None = (
            _build_voice(config.voice) if config.voice is not None else None
        )
        self._compactor = Compactor(
            provider,
            self._sessions,
            self._scheduler,
            tiers,
            is_busy=self._is_busy,
        )
        self._coding: CodingStack | None = (
            CodingStack(
                config.coding,
                upstream_base_url=config.provider.base_url,
                upstream_api_key=config.provider.api_key,
                notifier=QqNotifier(self._send, self._sessions, self._qq_account_of),
                queue_path=storage.for_module("coding"),
            )
            if config.coding is not None
            else None
        )
        registry = builtin_tools()
        for spec in memory_tools(self._memory):
            registry.register(spec)
        for spec in search_tools(self._search):
            registry.register(spec)
        if self._voice is not None:
            registry.register(self._send_voice_tool(self._voice))
        if self._coding is not None:
            for spec in self._coding.tools:
                registry.register(spec)
        # omp tools are private-only: the group registry is built separately and never sees them.
        for spec in self._omp.tools():
            registry.register(spec)
        self._runner = AgentRunner(
            provider=provider,
            store=self._sessions,
            tools=registry,
            instructions=self._private_instructions,
            sender=self._send,
            reasoning_effort=effort_for("private", tiers),
            on_response=self._on_response,
        )
        self._dispatcher = PrivateDispatcher(
            self._runner, timebase, on_turn_done=self._on_turn_done
        )

        # Group path (M3). The gate needs the bot's QQ id, known only after NapCat connects,
        # so it is built lazily on the first group message (see _group_gate).
        group_cfg = config.group
        self._group_state = GroupStateStore(storage.for_module("group_state"))
        self._group_skills = GroupSkillStore(storage.for_module("group_skill"))
        self._gate: GroupGate | None = None
        self._groups = GroupSessionManager(
            provider=provider,
            sessions=self._sessions,
            skills=self._group_skills,
            sender=self._send_group,
            profiles=_MemoryProfileSink(self._memory),
            limits=SkillLimits(
                topics=group_cfg.skill_topics,
                memes=group_cfg.skill_memes,
                rules=group_cfg.skill_rules,
                members=group_cfg.skill_members,
                habits_per_member=group_cfg.skill_habits_per_member,
            ),
            policy=GroupSessionPolicy(
                idle_timeout_s=group_cfg.idle_timeout_seconds,
                max_turns=group_cfg.max_turns,
            ),
            chat_reasoning_effort=effort_for("group", tiers),
            summary_reasoning_effort=effort_for("summary", tiers),
        )
        self._tasks: set[asyncio.Task[object]] = set()
        self._sweep_task: asyncio.Task[None] | None = None
        self._closing = False

    async def start(self) -> None:
        await self._groups.restore()
        if self._coding is not None:
            await self._coding.start()
        await self._omp.start()
        await self.napcat.start()
        await self._scheduler.start()
        self._sweep_task = asyncio.create_task(self._sweep_loop(), name="group-sweep")
        log.info("cgbot started: napcat_port=%s", self.napcat.bound_port)

    async def stop(self) -> None:
        self._closing = True
        if self._sweep_task is not None:
            await _cancel_tasks([self._sweep_task])
        await self.napcat.stop()
        await self._dispatcher.close()
        await _cancel_tasks(list(self._tasks))
        await self._omp.close()
        if self._coding is not None:
            await self._coding.stop()
        await self._scheduler.stop()
        self._scheduler.close()
        await self._sessions.close()
        await self._identity.close()
        await self._memory.close()
        await self._search.close()
        self._group_state.close()
        self._group_skills.close()
        self._omp_store.close()
        if self._voice is not None:
            for service in self._voice.services:
                await service.aclose()

    async def handle_message(self, message: InboundMessage) -> None:
        if self._closing:
            return
        if message.scope == "group":
            await self._handle_group(message)
            return
        if not self._config.scope.allows("private", message.user_id, None):
            log.info("private message out of scope dropped: message_id=%s", message.message_id)
            return
        text = await self._render_private(message.segments)
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

    async def _qq_account_of(self, user_id: str) -> str:
        """QQ account of an internal user's preferred channel (coding notices go there)."""
        return (await self._identity.preferred_channel(user_id)).account

    async def wake_private(self, user_id: str, text: str) -> None:
        """Deliver a background notice (e.g. an omp report) into a user's private session.

        Enqueues and returns at once: the turn runs in the private lane after any running turn,
        never interrupting it. Unknown users are logged and dropped.
        """
        if self._closing:
            return
        identity = await self._identity.identity_for_user(user_id)
        if identity is None:
            log.warning("private wake dropped: unknown user")
            return
        session_id = await self._identity.private_session_id(identity.user_id)
        channel = await self._identity.preferred_channel(identity.user_id)
        self._dispatcher.on_wake(
            PrivateMessage(
                session_id=session_id,
                user_id=identity.user_id,
                message_id=None,
                item=user_message(PRIVATE_NOTICE_PREFIX + text),
                channel=channel,
                channels=identity.channels,
            )
        )

    async def _handle_group(self, message: InboundMessage) -> None:
        gid = message.group_id
        if gid is None:
            return
        if not self._config.scope.allows("group", message.user_id, gid):
            log.info("group message out of scope dropped: message_id=%s", message.message_id)
            return
        gate = self._group_gate()
        if gate is None:
            log.warning("group message dropped, bot id unknown: message_id=%s", message.message_id)
            return
        decision = await gate.decide(message)
        if decision.action == "mute_triggered":
            self._spawn(self._groups.mute(gid))
            return
        trigger = _GROUP_TRIGGERS.get(decision.action)
        if trigger is None:
            return
        # Taken synchronously before the turn starts: the context now belongs to this turn.
        buffered = gate.take_context(gid)
        self._spawn(self._groups.handle(message, trigger, buffered=buffered))

    def _group_gate(self) -> GroupGate | None:
        if self._gate is None:
            bot_id = self.napcat.self_id
            if bot_id is None:
                return None
            self._gate = GroupGate(
                scope=self._config.scope,
                group=self._config.group,
                bot_id=bot_id,
                store=self._group_state,
                clock=time.time,
                rng=random.Random(),
            )
        return self._gate

    def _spawn(self, coro: Coroutine[Any, Any, object]) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task[object]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error("background task failed", exc_info=exc)

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(GROUP_SWEEP_SECONDS)
            try:
                await self._groups.sweep()
            except Exception:
                log.exception("group sweep failed")

    async def _render_private(self, segments: Sequence[Segment]) -> str:
        record_texts: dict[int, str] = {}
        for idx, seg in enumerate(segments):
            if seg.type == "record":
                record_texts[idx] = await self._record_text(seg)
        return render_segments(segments, record_texts)

    async def _record_text(self, seg: Segment) -> str:
        """Private voice -> transcript. Fail-soft: any failure yields ``RECORD_FAILED``."""
        voice = self._voice
        if voice is None:
            return _PLACEHOLDERS["record"]
        try:
            response = await self.napcat.call(
                "get_record", {"file": seg.data.get("file"), "out_format": "wav"}
            )
            audio = await asyncio.to_thread(_record_local_path(response).read_bytes)
            transcript = await voice.asr.transcribe(await voice.media.to_asr_wav(audio))
        except Exception as exc:
            log.warning("voice transcription failed: %s", type(exc).__name__)
            return RECORD_FAILED
        text = transcript.text.strip()
        return f"[语音转写: {text}]" if text else RECORD_FAILED

    def _send_voice_tool(self, voice: _Voice) -> ToolSpec:
        async def handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
            text = args.get("text")
            if not isinstance(text, str) or not text.strip():
                return ToolResult(output="text is required", is_error=True)
            try:
                synthesis = await voice.tts.synthesize(text)
                uri = await voice.media.to_record_uri(synthesis.wav)
            except Exception as exc:
                log.warning("voice synthesis failed: %s", type(exc).__name__)
                return ToolResult(
                    output=f"voice synthesis failed: {type(exc).__name__}", is_error=True
                )
            result = await ctx.sender(
                OutboundMessage(
                    scope="private",
                    target_id=ctx.source_channel.account,
                    segments=(Segment("record", {"file": uri}),),
                )
            )
            if not result.ok:
                return ToolResult(output="voice send failed", is_error=True)
            return ToolResult(output="voice sent", is_error=False)

        return ToolSpec(
            name="send_voice",
            description="把一段文字合成为语音消息发送给用户。",
            parameters_schema={
                "type": "object",
                "properties": {"text": {"type": "string", "description": "要朗读的文字"}},
                "required": ["text"],
            },
            handler=handler,
        )

    def _is_busy(self, session_id: str) -> bool:
        return self._runner.is_running(session_id)

    async def _private_instructions(self, session_id: str) -> str:
        """Per-turn instructions: the system prompt plus what is remembered about this user."""
        user_id = session_id.removeprefix(PRIVATE_SESSION_PREFIX)
        entries = await self._memory.list_for_prompt(
            viewer=Viewer.private(user_id), budget_chars=MEMORY_PROMPT_CHARS
        )
        prompt = SYSTEM_PROMPT if self._coding is None else f"{SYSTEM_PROMPT}\n\n{CODING_PROMPT}"
        if not entries:
            return prompt
        return f"{prompt}\n\n{MEMORY_HEADER}\n{format_entries(entries)}"

    def _on_response(self, session_id: str, usage: Usage, through_seq: int) -> None:
        self._compactor.record_usage(session_id, usage, through_seq)

    async def _on_turn_done(self, session_id: str, result: TurnResult) -> None:
        self._compactor.note_activity(session_id)
        # The summary is a model call: run it as a background task so the next turn is not held.
        self._spawn(self._compact_after_turn(session_id))

    async def _compact_after_turn(self, session_id: str) -> None:
        try:
            await self._compactor.maybe_compact(session_id)
        except Exception:
            log.exception("compaction after turn failed: session_id=%s", session_id)

    async def _send(self, message: OutboundMessage) -> SendResult:
        return await self.napcat.send(message)

    async def _send_group(self, message: OutboundMessage) -> SendResult:
        result = await self._send(message)
        gate = self._gate
        if result.ok and gate is not None and message.scope == "group":
            gate.note_bot_spoke(message.target_id, time.time(), result.message_id)
        return result

    async def _on_system_event(self, event: SystemEvent) -> None:
        if event.kind == KIND_COMPACTION_IDLE:
            # Compaction is a model call; hand off so the scheduler handler stays within its 30 s.
            self._spawn(self._run_idle_compaction(event))
            return
        log.info(
            "system event received (unrouted): event_id=%s kind=%s",
            event.event_id,
            event.kind,
        )

    async def _run_idle_compaction(self, event: SystemEvent) -> None:
        outcome = await self._compactor.handle_idle_event(event)
        log.info(
            "compaction idle event handled: event_id=%s status=%s",
            event.event_id,
            outcome.status,
        )


async def _cancel_tasks(tasks: Iterable[asyncio.Task[Any]]) -> None:
    pending = list(tasks)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
