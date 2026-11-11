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
from zoneinfo import ZoneInfo

from cgbot.agent import AgentRunner, TurnResult, builtin_tools
from cgbot.agent.tools import ToolContext, ToolResult, ToolSpec
from cgbot.character import CharacterCard
from cgbot.coding.notifier import QqNotifier
from cgbot.coding.stack import CodingStack
from cgbot.coding.staging import ImageStager, StageError
from cgbot.compaction import Compactor
from cgbot.config import Config
from cgbot.dispatch import PrivateDispatcher, PrivateMessage, Timebase
from cgbot.group.gate import GroupGate
from cgbot.group.session import GroupSessionManager, GroupSessionPolicy, GroupTrigger
from cgbot.group.skill import GroupSkillStore, ProfileEntry, SkillLimits
from cgbot.group.store import GroupStateStore
from cgbot.identity import AsyncIdentityStore
from cgbot.meme import MemeStore, meme_lookup_tool
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
from cgbot.search.gemini import GeminiSearchClient
from cgbot.search.xsearch import GrokXSearchClient, XSearchService
from cgbot.session import AsyncSessionStore
from cgbot.timefmt import time_instruction
from cgbot.vision import build_vision, segment_image_urls
from cgbot.vision.fetch import Fetch, default_fetch
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
IMAGE_PRUNE_SECONDS = 3600.0
SCHEDULER_HANDLER_TIMEOUT = 30.0  # round 20: per-attempt timeout
GROUP_SWEEP_SECONDS = 30.0
RECORD_FAILED = "[语音识别失败]"
PRIVATE_NOTICE_PREFIX = "[系统通知] "
# R1.6: fixed notice for a private turn that failed on the model with no reply sent (not via model).
MODEL_FAILURE_NOTICE = "（上游模型暂时连不上，这条没能回复，请稍后再发一次）"
_GROUP_TRIGGERS: Mapping[str, GroupTrigger] = {
    "wake": "wake",
    "followup": "followup",
    "proactive": "proactive",
}

CODING_PROMPT = (
    "你背后有一个 Coding Agent,通过 codex_* 工具使用,默认可以做任何事(调研、采集数据、写代码、"
    "操作浏览器、生成图片)。对方让你做需要动手完成的实事时,直接调用 codex_submit 交办,"
    "不要自己下结论说做不到,也不要先闲聊推脱;交办后用一两句人设口吻简短告知。"
    "任务结果会以[系统通知]消息送达,不要轮询;收到通知后用一两句人设口吻简短总结结果,"
    "不复述原文、不讲技术细节、不列清单。"
    "想知道进展时用 codex_btw 旁路询问(不打断、不排队),不要为此反复 codex_read 或 codex_peek。"
    "消息中标注已保存到沙盒的图片可按给出的 /workspace 路径交给 Coding Agent 使用。"
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
        image_fetch: Fetch = default_fetch,
    ) -> None:
        self._config = config
        # Character card loaded once at startup (prompt cache stability).
        self._card: CharacterCard = config.persona
        owner_id = config.scope.owner or "owner"
        # Render persona with {owner_id} placeholder injection (one-time, cache-stable).
        self._persona = config.persona.persona.replace("{owner_id}", owner_id)
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
        self._memory_blocks: dict[
            str, str
        ] = {}  # per-session memory prompt snapshot (cache-stable)
        self._search = SearchService(config.search)
        self._gemini: GeminiSearchClient | None = None
        self._coding_gemini: GeminiSearchClient | None = None
        gemini = config.search.gemini
        if gemini.enabled:
            # 同一上游中转、同一模型;bot 与沙盒桥各用自己的密钥(沙盒桥规则同 CodingStack 上游密钥)。
            self._gemini = GeminiSearchClient(
                gemini.base_url,
                config.provider.api_key.reveal(),
                gemini.model,
                gemini.timeout_seconds,
            )
            if config.coding is not None:
                self._coding_gemini = GeminiSearchClient(
                    gemini.base_url,
                    (config.coding.upstream_api_key or config.provider.api_key).reveal(),
                    gemini.model,
                    gemini.timeout_seconds,
                )
        self._x_search: XSearchService | None = None
        x = config.search.x
        if x.enabled:
            # Grok 原生 x_search 为主;[search.gemini] 启用时回退到 bot 自己的 Gemini 客户端。
            self._x_search = XSearchService(
                GrokXSearchClient(
                    x.base_url,
                    config.provider.api_key.reveal(),
                    x.model,
                    x.timeout_seconds,
                ),
                self._gemini,
            )
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
        # One zone for gate hours, log stamps and every timestamp the model sees.
        self._tz = ZoneInfo(config.group.timezone)
        self._voice: _Voice | None = (
            _build_voice(config.voice) if config.voice is not None else None
        )
        self._compactor = Compactor(
            provider,
            self._sessions,
            self._scheduler,
            tiers,
            is_busy=self._is_busy,
            tz=self._tz,
        )
        self._coding: CodingStack | None = (
            CodingStack(
                config.coding,
                upstream_base_url=config.provider.base_url,
                upstream_api_key=config.coding.upstream_api_key or config.provider.api_key,
                notifier=QqNotifier(
                    self._send,
                    self._sessions,
                    self._start_coding_notice_turn,
                    self._qq_account_of,
                ),
                queue_path=storage.for_module("coding"),
                search_service=self._search,
                gemini_client=self._coding_gemini,
            )
            if config.coding is not None
            else None
        )
        self._image_stager = (
            ImageStager(config.coding.docker.host_data_dir, fetch=image_fetch)
            if config.coding is not None
            else None
        )
        self._memes = MemeStore(storage.for_module("meme"))
        self._vision = build_vision(config.provider.vision, storage.images, fetch=image_fetch)
        registry = builtin_tools()
        for spec in memory_tools(self._memory):
            registry.register(spec)
        for spec in search_tools(
            self._search, gemini_client=self._gemini, x_service=self._x_search
        ):
            registry.register(spec)
        registry.register(meme_lookup_tool(self._memes))
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
            images=self._vision.store if self._vision is not None else None,
            recent_image_turns=self._vision.recent_image_turns if self._vision is not None else 0,
            failure_notice=MODEL_FAILURE_NOTICE,
            tz=self._tz,
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
            memes=self._memes,
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
            vision=self._vision,
            tz=self._tz,
        )
        self._tasks: set[asyncio.Task[object]] = set()
        self._sweep_task: asyncio.Task[None] | None = None
        self._closing = False

    async def start(self) -> None:
        await self._groups.restore()
        await self._prune_images()
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
        self._memes.close()
        self._omp_store.close()
        if self._voice is not None:
            for service in self._voice.services:
                await service.aclose()

    async def handle_message(self, message: InboundMessage) -> None:
        if self._closing:
            return
        if _is_foreign_command(message.segments):
            log.info("slash command ignored: message_id=%s", message.message_id)
            return
        if message.scope == "group":
            await self._handle_group(message)
            return
        if not self._config.scope.allows(
            "private", message.user_id, None, is_friend=message.is_friend
        ):
            log.info("private message out of scope dropped: message_id=%s", message.message_id)
            return
        text = await self._render_private(message.segments)
        if not text:
            log.info("empty private message ignored: message_id=%s", message.message_id)
            return
        channel = Channel("qq", message.user_id)
        identity = await self._identity.resolve(channel)
        notes = await self._stage_private_images(identity.user_id, message.segments)
        if notes:
            text += "\n" + "\n".join(notes)
        session_id = await self._identity.private_session_id(identity.user_id)
        item = (
            await self._vision.attach(session_id, text, segment_image_urls(message.segments))
            if self._vision is not None
            else user_message(text)
        )
        self._dispatcher.on_message(
            PrivateMessage(
                session_id=session_id,
                user_id=identity.user_id,
                message_id=message.message_id,
                item=item,
                channel=channel,
                channels=identity.channels,
            )
        )

    async def handle_input_status(self, status: InputStatus) -> None:
        if self._closing:
            return
        channel = Channel("qq", status.user_id)
        # input_status carries no sub_type. With all_friends, a user counts as a friend only when
        # an identity already exists (created by an accepted private message; find never creates).
        known = self._config.scope.all_friends and (await self._identity.find(channel)) is not None
        if not self._config.scope.allows("private", status.user_id, None, is_friend=known):
            return
        identity = await self._identity.resolve(channel)
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

    async def _start_coding_notice_turn(self, user_id: str, session_id: str) -> None:
        """Queue one private turn over a Coding Agent notice already recorded in the session.

        The notice record is the turn's input, so the item is None. The turn runs in the private
        lane like any wake: after a running turn and the quiet window, never interrupting it.
        Raises when no turn can be queued, so the notifier sends its plain-text fallback instead.
        """
        if self._closing:
            raise RuntimeError("app is closing")
        identity = await self._identity.identity_for_user(user_id)
        if identity is None:
            raise LookupError("unknown user")
        if session_id != await self._identity.private_session_id(identity.user_id):
            raise ValueError("session does not belong to user")
        channel = await self._identity.preferred_channel(identity.user_id)
        queued = self._dispatcher.on_wake(
            PrivateMessage(
                session_id=session_id,
                user_id=identity.user_id,
                message_id=None,
                item=None,
                channel=channel,
                channels=identity.channels,
            )
        )
        if not queued:
            raise RuntimeError("private dispatcher is closing")

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
                tz=self._tz,
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
        last_prune = time.monotonic()
        while True:
            await asyncio.sleep(GROUP_SWEEP_SECONDS)
            try:
                await self._groups.sweep()
            except Exception:
                log.exception("group sweep failed")
            if self._vision is not None and time.monotonic() - last_prune >= IMAGE_PRUNE_SECONDS:
                last_prune = time.monotonic()
                await self._prune_images()

    async def _prune_images(self) -> None:
        if self._vision is None:
            return
        try:
            removed = await self._vision.store.prune()
        except Exception:
            log.exception("chat image prune failed")
        else:
            if removed:
                log.info("chat image retention removed files: count=%d", removed)

    async def _stage_private_images(self, user_id: str, segments: Sequence[Segment]) -> list[str]:
        if self._coding is None or self._image_stager is None:
            return []
        notes: list[str] = []
        for segment in segments:
            if segment.type != "image":
                continue
            url = segment.data.get("url") or segment.data.get("file")
            if not isinstance(url, str):
                log.warning("image staging failed: code=E_URL")
                continue
            try:
                path = await self._image_stager.stage(user_id, url)
            except StageError as exc:
                log.warning("image staging failed: code=%s", exc.code)
            except Exception:
                # URLs may contain credentials; never log exception details or the segment.
                log.warning("image staging failed: code=E_INTERNAL")
            else:
                notes.append(f"[图片已保存到沙盒: {path}]")
        return notes

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
        """System prompt plus a per-session snapshot of what is remembered about this user.

        The memory block is read once per session window and kept byte-identical until the window
        is compacted (or the process restarts), so memories saved mid-session never rewrite the
        instructions prefix and the provider's prompt cache stays valid. A memory saved during the
        window is already in history as its tool call and is searchable via the memory tools.
        """
        prompt = f"{self._persona}\n\n{time_instruction(self._tz)}"
        if self._coding is not None:
            prompt += f"\n\n{CODING_PROMPT}"
        block = self._memory_blocks.get(session_id)
        if block is None:
            user_id = session_id.removeprefix(PRIVATE_SESSION_PREFIX)
            entries = await self._memory.list_for_prompt(
                viewer=Viewer.private(user_id), budget_chars=MEMORY_PROMPT_CHARS
            )
            block = f"\n\n{MEMORY_HEADER}\n{format_entries(entries)}" if entries else ""
            self._memory_blocks[session_id] = block
        return prompt + block

    def _on_response(self, session_id: str, usage: Usage, through_seq: int) -> None:
        self._compactor.record_usage(session_id, usage, through_seq)

    async def _on_turn_done(self, session_id: str, result: TurnResult) -> None:
        self._compactor.note_activity(session_id)
        # The summary is a model call: run it as a background task so the next turn is not held.
        self._spawn(self._compact_after_turn(session_id))

    async def _compact_after_turn(self, session_id: str) -> None:
        try:
            outcome = await self._compactor.maybe_compact(session_id)
            if outcome.status == "compacted":
                self._memory_blocks.pop(session_id, None)
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
        if outcome.status == "compacted":
            self._memory_blocks.pop(str(event.payload["session_id"]), None)
        log.info(
            "compaction idle event handled: event_id=%s status=%s",
            event.event_id,
            outcome.status,
        )


def _is_foreign_command(segments: Iterable[Segment]) -> bool:
    """True for a text message starting with "/" (other bots' commands), except "/reset".

    Leading at/reply segments are skipped, so "@bot /订阅 新闻" counts too.
    """
    for segment in segments:
        if segment.type in ("at", "reply"):
            continue
        if segment.type != "text":
            return False
        text = str(segment.data.get("text", "")).strip()
        if not text and segment.type == "text":
            continue
        if not text.startswith("/"):
            return False
        command = text.split(maxsplit=1)[0]
        return command.casefold() != "/reset"
    return False


async def _cancel_tasks(tasks: Iterable[asyncio.Task[Any]]) -> None:
    pending = list(tasks)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
