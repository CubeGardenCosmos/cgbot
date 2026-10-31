"""Private dispatcher: background wakes (``on_wake``) queue behind the lane and never interrupt.

The clock is fake; the runner is real (temporary SQLite file) and the provider is scripted.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from cgbot.agent import AgentRunner, builtin_tools
from cgbot.dispatch import QUIET_SECONDS, PrivateDispatcher, PrivateMessage
from cgbot.model import Channel, OutboundMessage, SendResult
from cgbot.provider import ModelRequest, ModelResponse, TextOutput, Usage, user_message
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


class GatedProvider:
    """The first request announces itself, then waits on ``gate``; later requests answer at once."""

    def __init__(self, *script: ModelResponse, gate: asyncio.Event) -> None:
        self._script = list(script)
        self._gate = gate
        self.started = asyncio.Event()
        self.requests: list[ModelRequest] = []

    @property
    def name(self) -> str:
        return "scripted"

    @property
    def context_window(self) -> int:
        return 100_000

    async def respond(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if len(self.requests) == 1:
            self.started.set()
            await self._gate.wait()
        return self._script.pop(0)

    def respond_stream(self, request: ModelRequest) -> AsyncIterator[Any]:
        raise NotImplementedError


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


async def _send(message: OutboundMessage) -> SendResult:
    return SendResult(ok=True, message_id=None, error=None)


def make_runner(store: AsyncSessionStore, provider: GatedProvider) -> AgentRunner:
    return AgentRunner(
        provider=provider,
        store=store,
        tools=builtin_tools(),
        instructions="be brief",
        sender=_send,
    )


def user_msg(message_id: str, text: str) -> PrivateMessage:
    return PrivateMessage(
        session_id=SID,
        user_id=USER,
        message_id=message_id,
        item=user_message(text),
        channel=SRC,
        channels=(),
    )


def wake(text: str) -> PrivateMessage:
    return PrivateMessage(
        session_id=SID,
        user_id=USER,
        message_id=None,
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


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[AsyncSessionStore]:
    s = AsyncSessionStore(tmp_path / "session.sqlite")
    yield s
    await s.close()


async def test_wake_waits_behind_running_turn_and_never_cancels_it(
    store: AsyncSessionStore,
) -> None:
    clock = FakeTimebase()
    gate = asyncio.Event()
    provider = GatedProvider(text_resp("第一条的回复"), text_resp("通知后的回复"), gate=gate)
    runner = make_runner(store, provider)
    cancelled: list[str] = []
    original_cancel = runner.cancel

    def spy_cancel(session_id: str) -> bool:
        cancelled.append(session_id)
        return original_cancel(session_id)

    runner.cancel = spy_cancel  # type: ignore[method-assign]
    dispatcher = PrivateDispatcher(runner, clock)

    dispatcher.on_message(user_msg("m1", "第一条"))
    clock.advance(QUIET_SECONDS)
    await asyncio.wait_for(provider.started.wait(), timeout=5)  # first turn is in flight

    dispatcher.on_wake(wake("[系统通知] 任务完成"))
    await flush()
    assert cancelled == []  # the wake did not interrupt the running turn

    gate.set()
    await drain(dispatcher, clock)

    assert cancelled == []
    assert len(provider.requests) == 2
    assert "任务完成" in json.dumps(provider.requests[1].input, ensure_ascii=False)
    await dispatcher.close()


async def test_wake_on_idle_lane_runs_after_quiet_window(store: AsyncSessionStore) -> None:
    clock = FakeTimebase()
    gate = asyncio.Event()
    gate.set()  # not blocking: this turn answers at once
    provider = GatedProvider(text_resp("收到"), gate=gate)
    dispatcher = PrivateDispatcher(make_runner(store, provider), clock)

    dispatcher.on_wake(wake("[系统通知] 后台任务完成"))
    await flush()
    assert provider.requests == []  # quiet window not yet over

    await drain(dispatcher, clock)

    assert len(provider.requests) == 1
    assert "后台任务完成" in json.dumps(provider.requests[0].input, ensure_ascii=False)
    stored = await store.load(SID)
    assert [i.kind for i in stored if i.kind == "user"] == ["user"]
    await dispatcher.close()


async def test_wake_after_shutdown_is_dropped(store: AsyncSessionStore) -> None:
    clock = FakeTimebase()
    provider = GatedProvider(gate=asyncio.Event())
    dispatcher = PrivateDispatcher(make_runner(store, provider), clock)
    await dispatcher.close()

    dispatcher.on_wake(wake("[系统通知] 迟到的通知"))
    await flush()

    assert dispatcher._lanes == {}  # noqa: SLF001 - test inspects lanes
    assert provider.requests == []


def notice_wake() -> PrivateMessage:
    """Coding notice trigger: the notice record is already stored, so the wake adds no item."""
    return PrivateMessage(
        session_id=SID,
        user_id=USER,
        message_id=None,
        item=None,
        channel=SRC,
        channels=(),
    )


async def test_notice_trigger_runs_turn_over_stored_record_without_extra_user_row(
    store: AsyncSessionStore,
) -> None:
    clock = FakeTimebase()
    gate = asyncio.Event()
    gate.set()  # not blocking: this turn answers at once
    provider = GatedProvider(text_resp("好的"), gate=gate)
    dispatcher = PrivateDispatcher(make_runner(store, provider), clock)
    await store.append(
        SID, "system_event", {"event": "coding_notice", "item": user_message("[系统通知] 任务完成")}
    )

    assert dispatcher.on_wake(notice_wake()) is True
    await flush()
    assert provider.requests == []  # quiet window not yet over

    await drain(dispatcher, clock)

    assert len(provider.requests) == 1
    assert "任务完成" in json.dumps(provider.requests[0].input, ensure_ascii=False)
    stored = await store.load(SID)
    assert [i.kind for i in stored if i.kind == "user"] == []
    assert [i.kind for i in stored if i.kind == "system_event"] == ["system_event"]
    await dispatcher.close()


async def test_notice_trigger_waits_behind_running_turn_and_reads_the_record(
    store: AsyncSessionStore,
) -> None:
    clock = FakeTimebase()
    gate = asyncio.Event()
    provider = GatedProvider(text_resp("第一条的回复"), text_resp("已转告"), gate=gate)
    runner = make_runner(store, provider)
    cancelled: list[str] = []
    original_cancel = runner.cancel

    def spy_cancel(session_id: str) -> bool:
        cancelled.append(session_id)
        return original_cancel(session_id)

    runner.cancel = spy_cancel  # type: ignore[method-assign]
    dispatcher = PrivateDispatcher(runner, clock)

    dispatcher.on_message(user_msg("m1", "第一条"))
    clock.advance(QUIET_SECONDS)
    await asyncio.wait_for(provider.started.wait(), timeout=5)  # first turn is in flight

    await store.append(
        SID, "system_event", {"event": "coding_notice", "item": user_message("[系统通知] 任务完成")}
    )
    assert dispatcher.on_wake(notice_wake()) is True
    await flush()
    assert cancelled == []  # the trigger did not interrupt the running turn

    gate.set()
    await drain(dispatcher, clock)

    assert cancelled == []
    assert len(provider.requests) == 2
    assert "任务完成" in json.dumps(provider.requests[1].input, ensure_ascii=False)
    await dispatcher.close()


async def test_notice_trigger_reports_dropped_wake_after_shutdown(
    store: AsyncSessionStore,
) -> None:
    clock = FakeTimebase()
    provider = GatedProvider(gate=asyncio.Event())
    dispatcher = PrivateDispatcher(make_runner(store, provider), clock)
    await dispatcher.close()

    assert dispatcher.on_wake(notice_wake()) is False
    await flush()
    assert provider.requests == []
