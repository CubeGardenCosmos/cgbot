from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from cgbot.agent import ToolContext, ToolRegistry
from cgbot.group import (
    BufferedMessage,
    GroupSessionManager,
    GroupSessionPolicy,
    GroupSkillStore,
    InMemoryProfileSink,
    SkillLimits,
    TurnControl,
    group_tools,
)
from cgbot.group.skill import SUMMARY_INSTRUCTIONS
from cgbot.group.tools import group_reply_tool
from cgbot.meme import MemeEntry, MemeStore
from cgbot.model import Channel, InboundMessage, OutboundMessage, Segment, SendResult
from cgbot.provider import (
    FunctionCall,
    ModelRequest,
    ModelResponse,
    TextOutput,
    Usage,
)
from cgbot.session import AsyncSessionStore
from cgbot.timefmt import format_stamp

GID = "5001"
SID = f"group:{GID}"
T0 = 1_000_000.0
LIMITS = SkillLimits(topics=5, memes=5, rules=5, members=3, habits_per_member=3)
TZ = ZoneInfo("Asia/Shanghai")


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> float:
        return self.now


def gmsg(mid: str, uid: str, text: str, gid: str = GID) -> InboundMessage:
    return InboundMessage(
        message_id=mid,
        scope="group",
        user_id=uid,
        group_id=gid,
        segments=(Segment("text", {"text": text}),),
        time=T0,
    )


def calls_resp(*calls: tuple[str, str, dict[str, Any]]) -> ModelResponse:
    items: list[Any] = []
    for call_id, name, args in calls:
        arguments = json.dumps(args)
        raw = {"type": "function_call", "call_id": call_id, "name": name, "arguments": arguments}
        items.append(
            FunctionCall(
                call_id=call_id,
                name=name,
                arguments=arguments,
                parsed_arguments=args,
                parse_error=None,
                raw=raw,
            )
        )
    return ModelResponse(items=tuple(items), usage=Usage(), response_id="r", status="completed")


def text_resp(text: str) -> ModelResponse:
    raw = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text}],
    }
    return ModelResponse(
        items=(TextOutput(text=text, raw=raw),),
        usage=Usage(),
        response_id="r",
        status="completed",
    )


def summary_json(atmosphere: str = "轻松", members: list[dict[str, Any]] | None = None) -> str:
    return json.dumps(
        {
            "atmosphere": atmosphere,
            "topics": ["游戏"],
            "memes": ["鱼"],
            "rules": ["别刷屏"],
            "members": members if members is not None else [],
        },
        ensure_ascii=False,
    )


class FakeProvider:
    """Scripted chat responses; summary calls are recognised by their instructions."""

    def __init__(
        self,
        chat: list[ModelResponse | Exception] | None = None,
        summary: str | Exception | None = None,
    ) -> None:
        self._chat = list(chat or [])
        self._summary = summary if summary is not None else summary_json()
        self.chat_requests: list[ModelRequest] = []
        self.summary_calls = 0
        self.block: asyncio.Event | None = None
        self.started = asyncio.Event()

    @property
    def name(self) -> str:
        return "fake"

    @property
    def context_window(self) -> int:
        return 100_000

    async def respond(self, request: ModelRequest) -> ModelResponse:
        if request.instructions == SUMMARY_INSTRUCTIONS:
            self.summary_calls += 1
            if isinstance(self._summary, Exception):
                raise self._summary
            return text_resp(self._summary)
        self.chat_requests.append(request)
        self.started.set()
        if self.block is not None:
            await self.block.wait()
        if not self._chat:
            raise AssertionError("no scripted chat response left")
        step = self._chat.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    def respond_stream(self, request: ModelRequest) -> AsyncIterator[Any]:
        raise NotImplementedError


class RecordingSender:
    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []

    async def __call__(self, message: OutboundMessage) -> SendResult:
        self.sent.append(message)
        return SendResult(ok=True, message_id=f"out-{len(self.sent)}", error=None)


@pytest.fixture
async def sessions(tmp_path: Path) -> AsyncIterator[AsyncSessionStore]:
    store = AsyncSessionStore(tmp_path / "session.sqlite")
    yield store
    await store.close()


@pytest.fixture
def skills(tmp_path: Path) -> Iterator[GroupSkillStore]:
    store = GroupSkillStore(tmp_path / "group_skill.sqlite")
    yield store
    store.close()


@pytest.fixture
def memes(tmp_path: Path) -> Iterator[MemeStore]:
    store = MemeStore(tmp_path / "meme.sqlite")
    yield store
    store.close()


def make_manager(
    sessions: AsyncSessionStore,
    skills: GroupSkillStore,
    provider: FakeProvider,
    *,
    memes: MemeStore,
    sender: RecordingSender,
    clock: Clock,
    profiles: InMemoryProfileSink | None = None,
    policy: GroupSessionPolicy | None = None,
) -> GroupSessionManager:
    return GroupSessionManager(
        provider=provider,
        sessions=sessions,
        skills=skills,
        sender=sender,
        profiles=profiles if profiles is not None else InMemoryProfileSink(),
        memes=memes,
        limits=LIMITS,
        policy=policy or GroupSessionPolicy(),
        chat_reasoning_effort="low",
        summary_reasoning_effort="low",
        clock=clock,
        tz=TZ,
    )


def ctx(session_id: str, args_user: str = "u1") -> ToolContext:
    return ToolContext(
        user_id=args_user,
        session_id=session_id,
        source_channel=Channel("qq", GID),
        source_message_id="m1",
        sender=RecordingSender(),
        channels=(),
    )


# ---- tools -----------------------------------------------------------------------------------


async def test_reply_tool_sends_to_bound_group_with_at_and_quote() -> None:
    sender = RecordingSender()
    spec = group_reply_tool(GID, TurnControl())
    result = await spec.handler(
        ToolContext("u1", SID, Channel("qq", GID), "m1", sender, ()),
        {"text": "好", "quote": True, "at_sender": True},
    )
    assert result.is_error is False
    assert len(sender.sent) == 1
    out = sender.sent[0]
    assert (out.scope, out.target_id, out.reply_to) == ("group", GID, "m1")
    assert out.segments == (Segment("at", {"qq": "u1"}), Segment("text", {"text": "好"}))


async def test_reply_tool_refuses_private_or_other_group_session() -> None:
    sender = RecordingSender()
    spec = group_reply_tool(GID, TurnControl())
    for session_id in ("private:u1", "group:9999"):
        result = await spec.handler(
            ToolContext("u1", session_id, Channel("qq", GID), "m1", sender, ()),
            {"text": "hi"},
        )
        assert result.is_error is True
    assert sender.sent == []


@pytest.mark.parametrize(
    "args",
    [
        {"text": "x", "target_id": "u1"},
        {"text": "x", "scope": "private"},
        {"text": "x", "channel": "qq:1"},
        {"text": ""},
        {"text": "x", "quote": "yes"},
    ],
)
async def test_reply_tool_rejects_targeting_and_bad_args(args: dict[str, Any]) -> None:
    sender = RecordingSender()
    spec = group_reply_tool(GID, TurnControl())
    result = await spec.handler(ToolContext("u1", SID, Channel("qq", GID), "m1", sender, ()), args)
    assert result.is_error is True
    assert sender.sent == []


async def test_reply_tool_refuses_at_without_triggering_member() -> None:
    sender = RecordingSender()
    spec = group_reply_tool(GID, TurnControl())
    result = await spec.handler(
        ToolContext("", SID, Channel("qq", GID), None, sender, ()),
        {"text": "x", "at_sender": True},
    )
    assert result.is_error is True
    assert sender.sent == []


async def test_reply_tool_refused_after_end_in_same_turn() -> None:
    sender = RecordingSender()
    control = TurnControl()
    registry = group_tools(GID, control)
    end = registry.get("end_conversation")
    reply = registry.get("reply")
    assert end is not None and reply is not None
    await end.handler(ToolContext("u1", SID, Channel("qq", GID), "m1", sender, ()), {})
    result = await reply.handler(
        ToolContext("u1", SID, Channel("qq", GID), "m1", sender, ()), {"text": "bye"}
    )
    assert result.is_error is True
    assert sender.sent == []


def test_group_tools_have_no_channel_or_target_parameter() -> None:
    registry: ToolRegistry = group_tools(GID, TurnControl())
    schemas = registry.schemas(())
    names = {s["name"] for s in schemas}
    assert names == {"reply", "end_conversation"}
    for s in schemas:
        props = set(s["parameters"]["properties"])
        assert not props & {"channel", "target", "target_id", "scope", "group_id", "user_id"}


# ---- lifecycle -------------------------------------------------------------------------------


async def test_end_tool_ends_session_with_summary_and_stops_turn(
    sessions: AsyncSessionStore, skills: GroupSkillStore, memes: MemeStore
) -> None:
    members = [
        {"user_id": "u1", "role": "成员", "habits": ["爱吐槽"]},
        {"user_id": "ghost", "role": None, "habits": ["不该出现"]},
    ]
    provider = FakeProvider(
        chat=[
            calls_resp(
                ("c1", "reply", {"text": "再见"}),
                ("c2", "end_conversation", {}),
            )
        ],
        summary=summary_json("热闹", members),
    )
    sender = RecordingSender()
    profiles = InMemoryProfileSink()
    mgr = make_manager(
        sessions, skills, provider, memes=memes, sender=sender, clock=Clock(), profiles=profiles
    )

    result = await mgr.handle(gmsg("m1", "u1", "大肥鱼 再见"), "wake")

    assert result is not None and result.status == "completed"
    assert len(provider.chat_requests) == 1  # end guard: no second model call
    assert [m.segments for m in sender.sent] == [(Segment("text", {"text": "再见"}),)]
    assert not mgr.is_active(GID)
    assert provider.summary_calls == 1

    card = await skills.get_card(GID)
    assert card is not None and card.atmosphere == "热闹"
    assert [m.user_id for m in card.members] == ["u1"]
    assert card.last_session is not None and card.last_session.end_reason == "end_tool"
    assert await skills.active_sessions() == []

    assert [e.user_id for e in profiles.entries] == ["u1"]
    assert profiles.entries[0].domain == f"group:{GID}"

    items = await sessions.load(SID)
    assert items[-1].kind == "compaction"
    assert "模型结束对话" in items[-1].payload["summary"]


async def test_next_session_receives_card_in_instructions(
    sessions: AsyncSessionStore, skills: GroupSkillStore, memes: MemeStore
) -> None:
    provider = FakeProvider(
        chat=[calls_resp(("c1", "end_conversation", {})), text_resp("ok")],
        summary=summary_json("冷清但友好", [{"user_id": "u1", "role": None, "habits": ["夜猫"]}]),
    )
    sender = RecordingSender()
    mgr = make_manager(sessions, skills, provider, memes=memes, sender=sender, clock=Clock())
    await mgr.handle(gmsg("m1", "u1", "大肥鱼 在吗"), "wake")

    await mgr.handle(gmsg("m2", "u2", "大肥鱼 新一轮"), "wake")

    second = provider.chat_requests[-1]
    assert "冷清但友好" in second.instructions
    assert "夜猫" in second.instructions
    assert "本群 SKILL" in second.instructions


async def test_next_session_does_not_replay_previous_raw_turns(
    sessions: AsyncSessionStore, skills: GroupSkillStore, memes: MemeStore
) -> None:
    provider = FakeProvider(
        chat=[calls_resp(("c1", "end_conversation", {})), text_resp("ok")],
    )
    mgr = make_manager(
        sessions, skills, provider, memes=memes, sender=RecordingSender(), clock=Clock()
    )
    await mgr.handle(gmsg("m1", "u1", "旧消息内容XYZ"), "wake")

    await mgr.handle(gmsg("m2", "u2", "新消息"), "wake")

    input_text = json.dumps(provider.chat_requests[-1].input, ensure_ascii=False)
    assert "旧消息内容XYZ" not in input_text
    assert "新消息" in input_text


async def test_idle_sweep_ends_only_after_timeout(
    sessions: AsyncSessionStore, skills: GroupSkillStore, memes: MemeStore
) -> None:
    clock = Clock()
    provider = FakeProvider(chat=[text_resp("ok")])
    mgr = make_manager(
        sessions,
        skills,
        provider,
        memes=memes,
        sender=RecordingSender(),
        clock=clock,
        policy=GroupSessionPolicy(idle_timeout_s=600, max_turns=30),
    )
    await mgr.handle(gmsg("m1", "u1", "大肥鱼 hi"), "wake")

    clock.now += 599
    assert await mgr.sweep() == ()
    assert mgr.is_active(GID)

    clock.now += 1
    assert await mgr.sweep() == (GID,)
    assert not mgr.is_active(GID)
    rows = await skills.active_sessions()
    assert rows == []
    card = await skills.get_card(GID)
    assert card is not None and card.last_session is not None
    assert card.last_session.end_reason == "idle"


async def test_turn_limit_ends_session_after_max_turns(
    sessions: AsyncSessionStore, skills: GroupSkillStore, memes: MemeStore
) -> None:
    provider = FakeProvider(chat=[text_resp("a"), text_resp("b")])
    mgr = make_manager(
        sessions,
        skills,
        provider,
        memes=memes,
        sender=RecordingSender(),
        clock=Clock(),
        policy=GroupSessionPolicy(idle_timeout_s=600, max_turns=2),
    )
    await mgr.handle(gmsg("m1", "u1", "大肥鱼 1"), "wake")
    assert mgr.is_active(GID)
    await mgr.handle(gmsg("m2", "u1", "大肥鱼 2"), "followup")

    assert not mgr.is_active(GID)
    card = await skills.get_card(GID)
    assert card is not None and card.last_session is not None
    assert card.last_session.end_reason == "turn_limit"


async def test_mute_during_running_turn_cancels_without_summary(
    sessions: AsyncSessionStore, skills: GroupSkillStore, memes: MemeStore
) -> None:
    provider = FakeProvider(chat=[text_resp("never")])
    provider.block = asyncio.Event()
    sender = RecordingSender()
    mgr = make_manager(sessions, skills, provider, memes=memes, sender=sender, clock=Clock())

    task = asyncio.create_task(mgr.handle(gmsg("m1", "u1", "大肥鱼 闭嘴"), "wake"))
    await provider.started.wait()
    await mgr.mute(GID)
    result = await asyncio.wait_for(task, timeout=5)

    assert result is not None and result.status == "cancelled"
    assert provider.summary_calls == 0
    assert sender.sent == []
    assert not mgr.is_active(GID)
    assert await skills.get_card(GID) is None
    rows = await skills.active_sessions()
    assert rows == []
    items = await sessions.load(SID)
    assert items[-1].kind == "compaction"
    assert "静音" in items[-1].payload["summary"]
    assert "未生成总结" in items[-1].payload["summary"]


async def test_mute_without_running_turn_ends_session_without_summary(
    sessions: AsyncSessionStore, skills: GroupSkillStore, memes: MemeStore
) -> None:
    provider = FakeProvider(chat=[text_resp("ok")])
    mgr = make_manager(
        sessions, skills, provider, memes=memes, sender=RecordingSender(), clock=Clock()
    )
    await mgr.handle(gmsg("m1", "u1", "大肥鱼 hi"), "wake")

    await mgr.mute(GID)

    assert not mgr.is_active(GID)
    assert provider.summary_calls == 0
    assert await skills.get_card(GID) is None


@pytest.mark.parametrize(
    "summary",
    ["这不是 JSON", RuntimeError("upstream down")],
)
async def test_summary_failure_closes_session_and_writes_no_card(
    sessions: AsyncSessionStore,
    skills: GroupSkillStore,
    memes: MemeStore,
    summary: str | Exception,
) -> None:
    provider = FakeProvider(chat=[calls_resp(("c1", "end_conversation", {}))], summary=summary)
    profiles = InMemoryProfileSink()
    mgr = make_manager(
        sessions,
        skills,
        provider,
        memes=memes,
        sender=RecordingSender(),
        clock=Clock(),
        profiles=profiles,
    )

    await mgr.handle(gmsg("m1", "u1", "大肥鱼 结束"), "wake")

    assert not mgr.is_active(GID)
    assert await skills.get_card(GID) is None
    assert profiles.entries == []
    rows = await skills.active_sessions()
    assert rows == []
    items = await sessions.load(SID)
    assert "总结失败" in items[-1].payload["summary"]


async def test_group_history_never_appears_under_private_session(
    sessions: AsyncSessionStore, skills: GroupSkillStore, memes: MemeStore
) -> None:
    provider = FakeProvider(
        chat=[calls_resp(("c1", "reply", {"text": "群里说"}), ("c2", "end_conversation", {}))],
        summary=summary_json(),
    )
    mgr = make_manager(
        sessions, skills, provider, memes=memes, sender=RecordingSender(), clock=Clock()
    )
    await mgr.handle(gmsg("m1", "u1", "群消息 秘密"), "wake")

    assert await sessions.load("private:u1") == []
    assert await sessions.load("private:5001") == []
    group_items = await sessions.load(SID)
    assert group_items
    assert all(item.session_id == SID for item in group_items)


async def test_proactive_turn_uses_buffered_context_and_persists_participants(
    sessions: AsyncSessionStore, skills: GroupSkillStore, memes: MemeStore
) -> None:
    provider = FakeProvider(chat=[text_resp("no reply")])
    mgr = make_manager(
        sessions, skills, provider, memes=memes, sender=RecordingSender(), clock=Clock()
    )
    buffered = (BufferedMessage("b1", "u2", T0, "今天吃啥"),)

    await mgr.handle(gmsg("m9", "u9", "无关"), "proactive", buffered=buffered)

    text = json.dumps(provider.chat_requests[-1].input, ensure_ascii=False)
    assert "[成员 u2] 今天吃啥" in text
    rows = await skills.active_sessions()
    assert len(rows) == 1
    assert rows[0].participants == ("u2",)
    items = await sessions.load(SID)
    assert any(i.kind == "system_event" and "item" in i.payload for i in items)


async def test_restore_resumes_active_session_and_keeps_history(
    sessions: AsyncSessionStore, skills: GroupSkillStore, memes: MemeStore
) -> None:
    clock = Clock()
    first = make_manager(
        sessions,
        skills,
        FakeProvider(chat=[text_resp("a")]),
        memes=memes,
        sender=RecordingSender(),
        clock=clock,
    )
    await first.handle(gmsg("m1", "u1", "持久化之前的话"), "wake")

    provider = FakeProvider(chat=[text_resp("b")])
    second = make_manager(
        sessions, skills, provider, memes=memes, sender=RecordingSender(), clock=clock
    )
    await second.restore()
    assert second.is_active(GID)

    await second.handle(gmsg("m2", "u1", "重启后的话"), "followup")

    text = json.dumps(provider.chat_requests[-1].input, ensure_ascii=False)
    assert "持久化之前的话" in text
    rows = await skills.active_sessions()
    assert rows[0].turn_count == 2


async def test_reply_from_group_turn_is_stored_as_tool_call_in_group_session(
    sessions: AsyncSessionStore, skills: GroupSkillStore, memes: MemeStore
) -> None:
    provider = FakeProvider(
        chat=[calls_resp(("c1", "reply", {"text": "好的"})), text_resp("done")],
    )
    sender = RecordingSender()
    mgr = make_manager(sessions, skills, provider, memes=memes, sender=sender, clock=Clock())

    await mgr.handle(gmsg("m1", "u1", "大肥鱼 问个事"), "wake")

    assert len(sender.sent) == 1
    items = await sessions.load(SID)
    assert any(i.kind == "tool_call" and i.payload.get("name") == "reply" for i in items)
    assert mgr.is_active(GID)


async def test_group_session_exposes_meme_lookup_and_stores_summary_memes(
    sessions: AsyncSessionStore, skills: GroupSkillStore, memes: MemeStore
) -> None:
    summary = json.dumps(
        {
            "atmosphere": "轻松",
            "topics": [],
            "memes": ["大肥鱼: 群吉祥物", "躺平"],
            "rules": [],
            "members": [],
        },
        ensure_ascii=False,
    )
    provider = FakeProvider(chat=[calls_resp(("c1", "end_conversation", {}))], summary=summary)
    mgr = make_manager(
        sessions, skills, provider, memes=memes, sender=RecordingSender(), clock=Clock()
    )

    await mgr.handle(gmsg("m1", "u1", "大肥鱼 结束"), "wake")

    names = {tool["name"] for tool in provider.chat_requests[0].tools}
    assert "meme_lookup" in names
    assert await memes.lookup("大肥鱼", group_id=GID) == (MemeEntry("大肥鱼", "群吉祥物"),)
    assert await memes.lookup("躺平", group_id=GID) == (MemeEntry("躺平", ""),)


async def test_group_render_stamps_member_message_and_bot_reply(
    sessions: AsyncSessionStore, skills: GroupSkillStore, memes: MemeStore
) -> None:
    provider = FakeProvider(chat=[calls_resp(("c1", "reply", {"text": "嗯"})), text_resp("done")])
    mgr = make_manager(
        sessions, skills, provider, memes=memes, sender=RecordingSender(), clock=Clock()
    )

    await mgr.handle(gmsg("m1", "u1", "大肥鱼 在吗"), "wake")

    rows = await sessions.load(SID)
    user_ts = next(i.ts for i in rows if i.kind == "user")
    reply_ts = next(i.ts for i in rows if i.kind == "tool_result")
    rendered = provider.chat_requests[1].input
    user = next(i for i in rendered if i.get("role") == "user")
    assert user["content"][0] == {"type": "input_text", "text": f"{format_stamp(user_ts, TZ)} "}
    assert user["content"][1]["text"] == "[成员 u1] 大肥鱼 在吗"
    output = next(i for i in rendered if i["type"] == "function_call_output")
    assert output["call_id"] == "c1"
    assert output["output"] == f"{format_stamp(reply_ts, TZ)} sent"
    assert [i["type"] for i in rendered if i["type"].startswith("function_call")] == [
        "function_call",
        "function_call_output",
    ]


async def test_group_instructions_explain_the_timestamps(
    sessions: AsyncSessionStore, skills: GroupSkillStore, memes: MemeStore
) -> None:
    provider = FakeProvider(chat=[text_resp("ok")])
    mgr = make_manager(
        sessions, skills, provider, memes=memes, sender=RecordingSender(), clock=Clock()
    )

    await mgr.handle(gmsg("m1", "u1", "大肥鱼 hi"), "wake")

    assert "方括号时间" in provider.chat_requests[0].instructions
    assert "Asia/Shanghai" in provider.chat_requests[0].instructions


async def test_proactive_buffered_lines_carry_their_own_times(
    sessions: AsyncSessionStore, skills: GroupSkillStore, memes: MemeStore
) -> None:
    provider = FakeProvider(chat=[text_resp("no reply")])
    mgr = make_manager(
        sessions, skills, provider, memes=memes, sender=RecordingSender(), clock=Clock()
    )
    buffered = (
        BufferedMessage("b1", "u2", T0, "今天吃啥"),
        BufferedMessage("b2", "u3", T0 + 3600, "火锅"),
    )

    await mgr.handle(gmsg("m9", "u9", "无关"), "proactive", buffered=buffered)

    text = json.dumps(provider.chat_requests[-1].input, ensure_ascii=False)
    assert f"{format_stamp(T0, TZ)} [成员 u2] 今天吃啥" in text
    assert f"{format_stamp(T0 + 3600, TZ)} [成员 u3] 火锅" in text
