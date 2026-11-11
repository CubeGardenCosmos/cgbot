"""Private dispatcher: quiet window (R2.1) and steering / interrupt (R2.2), offline.

The clock is fake: ``advance`` moves time and wakes the lane's quiet-window sleep. The runner is
real (temporary SQLite file) and the provider is scripted.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from cgbot.agent import AgentRunner, ToolContext, ToolRegistry, ToolResult, ToolSpec, builtin_tools
from cgbot.dispatch import QUIET_SECONDS, PrivateDispatcher, PrivateMessage
from cgbot.model import Channel, OutboundMessage, SendResult
from cgbot.provider import (
    FunctionCall,
    ModelRequest,
    ModelResponse,
    TextOutput,
    Usage,
    user_message,
)
from cgbot.session import AsyncSessionStore

SRC = Channel("qq", "10001")
USER = "u1"
SID = "private:u1"


class FakeTimebase:
    """``advance`` moves time forward and wakes every sleeper whose deadline has passed."""

    def __init__(self) -> None:
        self._now = 0.0
        self._sleepers: list[tuple[float, asyncio.Future[None]]] = []

    def now(self) -> float:
        return self._now

    async def sleep(self, seconds: float) -> None:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._sleepers.append((self._now + seconds, future))
        await future

    def advance(self, seconds: float) -> None:
        self._now += seconds
        for deadline, future in self._sleepers:
            if deadline <= self._now and not future.done():
                future.set_result(None)
        self._sleepers = [(d, f) for d, f in self._sleepers if not f.done()]


class ScriptedProvider:
    """Answers from a script. With ``blocking``, the first request sets it and never returns."""

    def __init__(self, *script: ModelResponse, blocking: asyncio.Event | None = None) -> None:
        self._script = list(script)
        self._blocking = blocking
        self.requests: list[ModelRequest] = []

    @property
    def name(self) -> str:
        return "scripted"

    @property
    def context_window(self) -> int:
        return 100_000

    async def respond(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if self._blocking is not None and len(self.requests) == 1:
            self._blocking.set()
            await asyncio.Event().wait()
        return self._script.pop(0)

    def respond_stream(self, request: ModelRequest) -> AsyncIterator[Any]:
        raise NotImplementedError


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


def wait_tool(started: asyncio.Event, release: asyncio.Event) -> ToolSpec:
    async def fn(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        started.set()
        await release.wait()
        return ToolResult("ok")

    return ToolSpec("wait", "test tool", {"type": "object", "properties": {}}, fn)


async def _send(message: OutboundMessage) -> SendResult:
    return SendResult(ok=True, message_id=None, error=None)


def make_runner(
    store: AsyncSessionStore, provider: ScriptedProvider, tools: ToolRegistry | None = None
) -> AgentRunner:
    return AgentRunner(
        provider=provider,
        store=store,
        tools=tools if tools is not None else builtin_tools(),
        instructions="be brief",
        sender=_send,
        tz=ZoneInfo("Asia/Shanghai"),
    )


_STAMP_PART = re.compile(r"\[\d{4}-\d\d-\d\d \d\d:\d\d 周.\] ")


def unstamped(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``items`` without the leading timestamp part of user messages (their time is real)."""
    out: list[dict[str, Any]] = []
    for item in items:
        content = item.get("content")
        if (
            item.get("role") == "user"
            and isinstance(content, list)
            and content
            and _STAMP_PART.fullmatch(content[0].get("text", ""))
        ):
            item = {**item, "content": content[1:]}
        out.append(item)
    return out


def msg(message_id: str, text: str) -> PrivateMessage:
    return PrivateMessage(
        session_id=SID,
        user_id=USER,
        message_id=message_id,
        item=user_message(text),
        channel=SRC,
        channels=(),
    )


async def flush() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


async def drain(dispatcher: PrivateDispatcher, clock: FakeTimebase) -> None:
    """Advance past quiet windows until no lane is left, i.e. every turn has finished."""
    for _ in range(100):
        tasks = [
            lane.task
            for lane in dispatcher._lanes.values()  # noqa: SLF001 - test inspects lanes
            if lane.task is not None
        ]
        if not tasks:
            return
        clock.advance(QUIET_SECONDS)
        await asyncio.wait(tasks, timeout=1.0)
    raise AssertionError("dispatcher did not drain")


async def stored_user_ids(store: AsyncSessionStore) -> list[str]:
    return [str(i.payload["message_id"]) for i in await store.load(SID) if i.kind == "user"]


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[AsyncSessionStore]:
    s = AsyncSessionStore(tmp_path / "session.sqlite")
    yield s
    await s.close()


async def test_turn_waits_for_quiet_window(store: AsyncSessionStore) -> None:
    clock = FakeTimebase()
    provider = ScriptedProvider(text_resp("ok"))
    dispatcher = PrivateDispatcher(make_runner(store, provider), clock)

    dispatcher.on_message(msg("m1", "hi"))
    clock.advance(QUIET_SECONDS - 0.5)
    await flush()
    assert provider.requests == []

    clock.advance(0.5)
    await drain(dispatcher, clock)
    assert len(provider.requests) == 1
    await dispatcher.close()


async def test_typing_push_extends_quiet_window(store: AsyncSessionStore) -> None:
    clock = FakeTimebase()
    provider = ScriptedProvider(text_resp("ok"))
    dispatcher = PrivateDispatcher(make_runner(store, provider), clock)

    dispatcher.on_message(msg("m1", "hi"))  # quiet until t=5
    clock.advance(4)
    dispatcher.on_typing(SID)  # quiet until t=9
    clock.advance(1)  # t=5: the old deadline passes, the lane re-arms for t=9
    await flush()
    clock.advance(3)  # t=8
    await flush()
    assert provider.requests == []

    clock.advance(1)  # t=9
    await drain(dispatcher, clock)
    assert len(provider.requests) == 1
    await dispatcher.close()


async def test_continuous_typing_has_no_maximum_wait(store: AsyncSessionStore) -> None:
    clock = FakeTimebase()
    provider = ScriptedProvider(text_resp("ok"))
    dispatcher = PrivateDispatcher(make_runner(store, provider), clock)

    dispatcher.on_message(msg("m1", "hi"))
    for _ in range(10):  # 40 seconds of typing, one push every 4 seconds
        clock.advance(4)
        dispatcher.on_typing(SID)
        await flush()
    assert provider.requests == []

    await drain(dispatcher, clock)
    assert len(provider.requests) == 1
    await dispatcher.close()


async def test_typing_without_pending_message_is_ignored(store: AsyncSessionStore) -> None:
    clock = FakeTimebase()
    provider = ScriptedProvider()
    dispatcher = PrivateDispatcher(make_runner(store, provider), clock)

    dispatcher.on_typing(SID)
    await flush()
    assert dispatcher._lanes == {}  # noqa: SLF001 - no lane is created by typing alone
    assert provider.requests == []
    await dispatcher.close()


async def test_messages_in_quiet_window_merge_in_arrival_order(store: AsyncSessionStore) -> None:
    clock = FakeTimebase()
    provider = ScriptedProvider(text_resp("ok"))
    dispatcher = PrivateDispatcher(make_runner(store, provider), clock)

    dispatcher.on_message(msg("m1", "一"))
    clock.advance(2)
    dispatcher.on_message(msg("m2", "二"))  # re-arms the window to t=7
    clock.advance(3)  # t=5
    await flush()
    assert provider.requests == []

    clock.advance(2)  # t=7
    await drain(dispatcher, clock)
    assert len(provider.requests) == 1
    inp = unstamped(provider.requests[0].input)
    assert inp.index(user_message("一")) < inp.index(user_message("二"))
    # coalesced messages stay separate items, each with its own leading stamp
    users = [i for i in provider.requests[0].input if i.get("role") == "user"]
    assert [len(u["content"]) for u in users] == [2, 2]
    assert await stored_user_ids(store) == ["m1", "m2"]
    await dispatcher.close()


async def test_message_without_tool_interrupts_and_restarts_with_new_items_only(
    store: AsyncSessionStore,
) -> None:
    clock = FakeTimebase()
    started = asyncio.Event()
    provider = ScriptedProvider(text_resp("reply"), blocking=started)
    dispatcher = PrivateDispatcher(make_runner(store, provider), clock)

    dispatcher.on_message(msg("m1", "一"))
    clock.advance(QUIET_SECONDS)
    await asyncio.wait_for(started.wait(), timeout=5)

    dispatcher.on_message(msg("m2", "二"))  # no tool called yet: interrupt
    await drain(dispatcher, clock)

    assert len(provider.requests) == 2
    inp = unstamped(provider.requests[1].input)
    assert inp.count(user_message("一")) == 1
    assert inp.count(user_message("二")) == 1
    assert await stored_user_ids(store) == ["m1", "m2"]
    await dispatcher.close()


async def test_message_after_tool_call_is_injected_without_interrupt(
    store: AsyncSessionStore,
) -> None:
    clock = FakeTimebase()
    tool_started = asyncio.Event()
    release = asyncio.Event()
    tools = builtin_tools()
    tools.register(wait_tool(tool_started, release))
    provider = ScriptedProvider(calls_resp(("c1", "wait", {})), text_resp("done"))
    dispatcher = PrivateDispatcher(make_runner(store, provider, tools), clock)

    dispatcher.on_message(msg("m1", "一"))
    clock.advance(QUIET_SECONDS)
    await asyncio.wait_for(tool_started.wait(), timeout=5)

    dispatcher.on_message(msg("m2", "二"))  # tool already called: injected, not interrupted
    release.set()
    await drain(dispatcher, clock)

    assert len(provider.requests) == 2
    assert unstamped(provider.requests[1].input)[-1] == user_message("二")
    assert await stored_user_ids(store) == ["m1", "m2"]
    await dispatcher.close()


async def test_close_cancels_waiting_lane(store: AsyncSessionStore) -> None:
    clock = FakeTimebase()
    provider = ScriptedProvider(text_resp("never"))
    dispatcher = PrivateDispatcher(make_runner(store, provider), clock)

    dispatcher.on_message(msg("m1", "一"))
    await flush()
    await dispatcher.close()
    clock.advance(QUIET_SECONDS)
    await flush()

    assert dispatcher._lanes == {}  # noqa: SLF001
    assert provider.requests == []


async def test_close_cancels_running_turn(store: AsyncSessionStore) -> None:
    clock = FakeTimebase()
    started = asyncio.Event()
    provider = ScriptedProvider(text_resp("never"), blocking=started)
    runner = make_runner(store, provider)
    dispatcher = PrivateDispatcher(runner, clock)

    dispatcher.on_message(msg("m1", "一"))
    clock.advance(QUIET_SECONDS)
    await asyncio.wait_for(started.wait(), timeout=5)

    await asyncio.wait_for(dispatcher.close(), timeout=5)
    assert runner.is_running(SID) is False
    assert len(provider.requests) == 1


async def test_messages_after_close_are_dropped(store: AsyncSessionStore) -> None:
    clock = FakeTimebase()
    provider = ScriptedProvider(text_resp("never"))
    dispatcher = PrivateDispatcher(make_runner(store, provider), clock)

    await dispatcher.close()
    dispatcher.on_message(msg("m1", "一"))
    clock.advance(QUIET_SECONDS)
    await flush()

    assert dispatcher._lanes == {}  # noqa: SLF001
    assert provider.requests == []
    assert await stored_user_ids(store) == []
