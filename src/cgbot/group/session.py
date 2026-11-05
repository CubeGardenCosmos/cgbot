"""Group conversation sessions (R3.4): turns, lifecycle end conditions, summary and card.

One active session per group, id ``group:<gid>``. Every session starts with a ``system_event``
marker and ends with a ``compaction`` item whose summary is the session boundary text, so the
next session (same id) never replays the previous one's raw turns (``load_active`` starts after
the compaction). The summary of a finished session is merged into the group's SKILL card
(``skill.py``) and injected into the instructions of the next session.

End conditions: LLM ``end_conversation`` tool, mute (no summary, no model call), 10 min without
activity (``sweep``), 30 turns. Gate decisions (wake/followup/proactive, mute) are made upstream;
this module only consumes already-gated events.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from typing import Literal
from zoneinfo import ZoneInfo

from cgbot.agent import AgentRunner, TurnContext, TurnResult
from cgbot.group.buffer import BufferedMessage
from cgbot.group.skill import (
    GroupSkillStore,
    ProfileSink,
    SessionSummary,
    SkillCard,
    SkillLimits,
    merge_card,
    profile_entries,
    render_card_text,
    render_transcript,
    summarize_transcript,
)
from cgbot.group.text import plain_text
from cgbot.group.tools import TurnControl, group_session_id, group_tools
from cgbot.meme import MemeStore, meme_lookup_tool, parse_meme
from cgbot.model import Channel, InboundMessage, OutboundMessage, SendResult
from cgbot.provider import (
    ModelProvider,
    ModelRequest,
    ModelResponse,
    StreamEvent,
    Usage,
    user_message,
)
from cgbot.session import AsyncSessionStore
from cgbot.timefmt import format_stamp, time_instruction
from cgbot.vision import Vision, segment_image_urls

log = logging.getLogger(__name__)

GroupTrigger = Literal["wake", "proactive", "followup"]
EndReason = Literal["end_tool", "mute", "idle", "turn_limit"]
SummaryStatus = Literal["written", "failed", "skipped"]

_REASON_LABEL: dict[str, str] = {
    "end_tool": "模型结束对话",
    "mute": "静音",
    "idle": "空闲超时",
    "turn_limit": "轮数上限",
}

GROUP_INSTRUCTIONS = (
    "你是这个 QQ 群里的机器人。只能通过 reply 工具在本群发言,普通文本输出不会被发送。"
    "没有需要说的就不要调用 reply。想结束这段对话时调用 end_conversation。"
    "遇到可能是梗/黑话的内容时,自行调用 meme_lookup 查询,不要凭猜测。"
    "不要提及或推断私聊内容,也不要透露你看不到的信息。"
)

PROACTIVE_PROMPT = (
    "[系统] 你没有被点名,是随机时机想插话。以下是群里最近未处理的消息。"
    "只在你确实有话要说时调用 reply,否则什么也不做。"
)


@dataclass(frozen=True, slots=True)
class GroupSessionPolicy:
    idle_timeout_s: float = 600.0
    max_turns: int = 30


class _EndGuardProvider:
    """Wraps the model provider. Once the end tool ran in this turn, the next request returns an
    empty completed response, so the runner stops without another model call. This is a
    termination signal, not a fabricated answer: nothing is sent and the turn is closed."""

    def __init__(self, inner: ModelProvider, control: TurnControl) -> None:
        self._inner = inner
        self._control = control

    @property
    def name(self) -> str:
        return self._inner.name

    @property
    def context_window(self) -> int:
        return self._inner.context_window

    async def respond(self, request: ModelRequest) -> ModelResponse:
        if self._control.ending:
            return ModelResponse(items=(), usage=Usage(), response_id="", status="completed")
        return await self._inner.respond(request)

    def respond_stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        return self._inner.respond_stream(request)


@dataclass(slots=True)
class _Session:
    group_id: str
    session_id: str
    row_id: int
    first_seq: int
    last_activity: float
    turn_count: int
    participants: list[str]
    control: TurnControl
    runner: AgentRunner
    mute_requested: bool = False


def _boundary_text(reason: EndReason, summary: SessionSummary | None, status: SummaryStatus) -> str:
    base = f"群会话已结束(原因:{_REASON_LABEL[reason]})"
    if summary is not None:
        parts = [f"气氛:{summary.atmosphere or '(无)'}"]
        if summary.topics:
            parts.append("话题:" + "; ".join(summary.topics))
        return base + ";" + ";".join(parts) + "。"
    if status == "failed":
        return base + ",总结失败。"
    return base + ",未生成总结。"


def _instructions(base: str, card: SkillCard | None, tz: ZoneInfo) -> str:
    base += time_instruction(tz)
    if card is None:
        return base
    return f"{base}\n\n# 本群 SKILL(历次群聊总结,仅本群有效)\n{render_card_text(card)}"


class GroupSessionManager:
    """Drives group sessions. Use from one asyncio event loop."""

    def __init__(
        self,
        *,
        provider: ModelProvider,
        sessions: AsyncSessionStore,
        skills: GroupSkillStore,
        sender: Callable[[OutboundMessage], Awaitable[SendResult]],
        profiles: ProfileSink,
        memes: MemeStore,
        limits: SkillLimits,
        policy: GroupSessionPolicy,
        chat_reasoning_effort: str | None,
        summary_reasoning_effort: str | None,
        tz: ZoneInfo,
        clock: Callable[[], float] = time.time,
        vision: Vision | None = None,
    ) -> None:
        self._provider = provider
        self._sessions = sessions
        self._skills = skills
        self._sender = sender
        self._profiles = profiles
        self._memes = memes
        self._limits = limits
        self._policy = policy
        self._chat_effort = chat_reasoning_effort
        self._summary_effort = summary_reasoning_effort
        self._clock = clock
        self._vision = vision
        self._tz = tz
        self._active: dict[str, _Session] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, group_id: str) -> asyncio.Lock:
        lock = self._locks.get(group_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[group_id] = lock
        return lock

    def is_active(self, group_id: str) -> bool:
        return group_id in self._active

    async def restore(self) -> None:
        """Reload active ledger rows after a restart. They stay active until idle or mute."""
        for rec in await self._skills.active_sessions():
            if rec.group_id in self._active:
                continue
            self._active[rec.group_id] = await self._build_session(
                group_id=rec.group_id,
                row_id=rec.row_id,
                first_seq=rec.first_seq,
                last_activity=rec.last_activity,
                turn_count=rec.turn_count,
                participants=list(rec.participants),
            )

    async def handle(
        self,
        msg: InboundMessage,
        trigger: GroupTrigger,
        buffered: Sequence[BufferedMessage] = (),
    ) -> TurnResult | None:
        """Run one group turn for an already-gated event. Returns None if the session was muted
        before the turn started."""
        if msg.scope != "group" or msg.group_id is None:
            raise ValueError("group session accepts group messages only")
        gid = msg.group_id
        async with self._lock(gid):
            session = self._active.get(gid)
            if session is None:
                session = await self._start(gid, trigger)
            session.last_activity = self._clock()
            session.turn_count += 1
            speakers = [msg.user_id] if trigger != "proactive" else []
            speakers.extend(b.user_id for b in buffered)
            for uid in speakers:
                if uid and uid not in session.participants:
                    session.participants.append(uid)
            await self._persist(session)
            if session.mute_requested:
                await self._end(session, "mute", summarize=False)
                return None
            ctx = await self._turn_context(session, msg, trigger, buffered)
            if session.mute_requested:  # a mute arrived while the images were being fetched
                await self._end(session, "mute", summarize=False)
                return None
            result = await session.runner.run_turn(ctx)
            session.last_activity = self._clock()
            await self._persist(session)
            if session.mute_requested:
                await self._end(session, "mute", summarize=False)
            elif session.control.ending:
                await self._end(session, "end_tool", summarize=True)
            elif session.turn_count >= self._policy.max_turns:
                await self._end(session, "turn_limit", summarize=True)
            return result

    async def mute(self, group_id: str) -> None:
        """Mute: end the session without summary or model call. Cancels a running turn."""
        session = self._active.get(group_id)
        if session is None:
            return
        session.mute_requested = True
        if session.runner.is_running(session.session_id):
            session.runner.cancel(session.session_id)
        async with self._lock(group_id):
            if self._active.get(group_id) is session:
                await self._end(session, "mute", summarize=False)

    async def sweep(self) -> tuple[str, ...]:
        """End sessions idle for ``idle_timeout_s`` (with summary). Returns the ended group ids."""
        ended: list[str] = []
        for gid in list(self._active):
            async with self._lock(gid):
                session = self._active.get(gid)
                if session is None or session.runner.is_running(session.session_id):
                    continue
                if self._clock() - session.last_activity >= self._policy.idle_timeout_s:
                    await self._end(session, "idle", summarize=True)
                    ended.append(gid)
        return tuple(ended)

    async def _turn_context(
        self,
        session: _Session,
        msg: InboundMessage,
        trigger: GroupTrigger,
        buffered: Sequence[BufferedMessage],
    ) -> TurnContext:
        # ChannelKind is "qq" only; group turns use the group's QQ account as the channel account.
        channel = Channel("qq", session.group_id)
        vision = self._vision
        if trigger == "proactive":
            lines = [
                f"{format_stamp(b.ts, self._tz)} [成员 {b.user_id}] {b.text}" for b in buffered
            ]
            text = PROACTIVE_PROMPT + "\n" + "\n".join(lines)
            item = (
                await vision.attach(
                    session.session_id,
                    text,
                    [url for b in buffered for url in b.image_urls],
                    newest=True,
                )
                if vision is not None
                else user_message(text)
            )
            return TurnContext(
                user_id="",
                session_id=session.session_id,
                source_channel=channel,
                source_message_id=None,
                new_items=[item],
                reason="system_event",
            )
        text = f"[成员 {msg.user_id}] {plain_text(msg.segments)}"
        item = (
            await vision.attach(session.session_id, text, segment_image_urls(msg.segments))
            if vision is not None
            else user_message(text)
        )
        return TurnContext(
            user_id=msg.user_id,
            session_id=session.session_id,
            source_channel=channel,
            source_message_id=msg.message_id,
            new_items=[item],
            reason="user",
        )

    async def _start(self, group_id: str, trigger: GroupTrigger) -> _Session:
        sid = group_session_id(group_id)
        now = self._clock()
        first_seq = await self._sessions.append(
            sid,
            "system_event",
            {"event": "group_session_started", "group_id": group_id, "trigger": trigger},
        )
        row_id = await self._skills.open_session(
            group_id=group_id, session_id=sid, first_seq=first_seq, now=now
        )
        session = await self._build_session(
            group_id=group_id,
            row_id=row_id,
            first_seq=first_seq,
            last_activity=now,
            turn_count=0,
            participants=[],
        )
        self._active[group_id] = session
        return session

    async def _build_session(
        self,
        *,
        group_id: str,
        row_id: int,
        first_seq: int,
        last_activity: float,
        turn_count: int,
        participants: list[str],
    ) -> _Session:
        card = await self._skills.get_card(group_id)
        control = TurnControl()
        tools = group_tools(group_id, control)
        tools.register(meme_lookup_tool(self._memes))
        runner = AgentRunner(
            _EndGuardProvider(self._provider, control),
            self._sessions,
            tools,
            _instructions(GROUP_INSTRUCTIONS, card, self._tz),
            self._sender,
            reasoning_effort=self._chat_effort,
            images=self._vision.store if self._vision is not None else None,
            recent_image_turns=self._vision.recent_image_turns if self._vision is not None else 0,
            tz=self._tz,
        )
        return _Session(
            group_id=group_id,
            session_id=group_session_id(group_id),
            row_id=row_id,
            first_seq=first_seq,
            last_activity=last_activity,
            turn_count=turn_count,
            participants=participants,
            control=control,
            runner=runner,
        )

    async def _persist(self, session: _Session) -> None:
        await self._skills.update_session(
            session.row_id,
            participants=session.participants,
            turn_count=session.turn_count,
            last_activity=session.last_activity,
        )

    async def _end(self, session: _Session, reason: EndReason, *, summarize: bool) -> None:
        """Close the session: optional summary, boundary item, ledger row, drop from active."""
        now = self._clock()
        summary: SessionSummary | None = None
        status: SummaryStatus = "skipped"
        if summarize:
            try:
                summary = await self._summarize_and_store(session, reason, now)
                status = "written"
            except Exception as exc:  # model, parse, or store errors: session must still close
                status = "failed"
                log.warning("group summary failed: %s", type(exc).__name__)
        await self._sessions.append(
            session.session_id,
            "compaction",
            {"summary": _boundary_text(reason, summary, status), "reason": reason},
        )
        await self._skills.finish_session(
            session.row_id, end_reason=reason, summary_status=status, now=now
        )
        self._active.pop(session.group_id, None)

    async def _summarize_and_store(
        self, session: _Session, reason: EndReason, now: float
    ) -> SessionSummary:
        items = await self._sessions.load(session.session_id, after_seq=session.first_seq - 1)
        summary = await summarize_transcript(
            self._provider, render_transcript(items), reasoning_effort=self._summary_effort
        )
        members = tuple(m for m in summary.members if m.user_id in session.participants)
        summary = replace(summary, members=members)
        prev = await self._skills.get_card(session.group_id)
        card = merge_card(
            prev,
            summary,
            group_id=session.group_id,
            session_id=session.session_id,
            now=now,
            end_reason=reason,
            limits=self._limits,
        )
        await self._skills.put_card(card)
        meme_entries = [parse_meme(m) for m in summary.memes if m.strip()]
        if meme_entries:
            try:
                await self._memes.upsert_many(session.group_id, meme_entries, now=now)
            except Exception as exc:  # best-effort: the card is already stored
                log.warning("group meme write failed: %s", type(exc).__name__)
        entries = profile_entries(
            group_id=session.group_id,
            session_id=session.session_id,
            ended_at=now,
            summary=summary,
        )
        if entries:
            try:
                await self._profiles.write(entries)
            except Exception as exc:  # best-effort: the card is already stored
                log.warning("group profile write failed: %s", type(exc).__name__)
        return summary
