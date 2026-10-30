from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from cgbot.agent import (
    COMPACTION_PREFIX,
    AgentRunner,
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    TurnContext,
    TurnInProgressError,
    builtin_tools,
)
from cgbot.agent.history import CANCELLED_OUTPUT, INTERRUPTED_OUTPUT
from cgbot.model import Channel, OutboundMessage, SendResult
from cgbot.provider import (
    FunctionCall,
    ModelRequest,
    ModelResponse,
    OtherOutput,
    ProviderError,
    Reasoning,
    TextOutput,
    UpstreamUnavailable,
    Usage,
    user_message,
)
from cgbot.session import AsyncSessionStore, Item, Kind

SRC = Channel("qq", "10001")
OTHER = Channel("qq", "20002")
USER = "u1"
SID = "private:u1"
IN_MSG = "m-in-1"


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


class ScriptedProvider:
    """Returns scripted responses (or raises scripted exceptions) and records requests."""

    def __init__(self, *script: ModelResponse | Exception) -> None:
        self._script = list(script)
        self.requests: list[ModelRequest] = []

    @property
    def name(self) -> str:
        return "scripted"

    @property
    def context_window(self) -> int:
        return 100_000

    async def respond(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if not self._script:
            raise AssertionError("no scripted response left")
        step = self._script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    def respond_stream(self, request: ModelRequest) -> AsyncIterator[Any]:
        raise NotImplementedError


class RecordingSender:
    def __init__(self, ok: bool = True) -> None:
        self.sent: list[OutboundMessage] = []
        self.ok = ok

    async def __call__(self, message: OutboundMessage) -> SendResult:
        self.sent.append(message)
        if not self.ok:
            return SendResult(ok=False, message_id=None, error="boom")
        return SendResult(ok=True, message_id=f"out-{len(self.sent)}", error=None)


def make_runner(
    store: AsyncSessionStore,
    provider: ScriptedProvider,
    sender: RecordingSender,
    tools: ToolRegistry | None = None,
) -> AgentRunner:
    return AgentRunner(
        provider=provider,
        store=store,
        tools=tools if tools is not None else builtin_tools(),
        instructions="be brief",
        sender=sender,
    )


def turn(
    text: str = "hi",
    *,
    reason: str = "user",
    channels: tuple[Channel, ...] = (),
    message_id: str | None = IN_MSG,
) -> TurnContext:
    return TurnContext(
        user_id=USER,
        session_id=SID,
        source_channel=SRC,
        source_message_id=message_id,
        new_items=[user_message(text)],
        reason=reason,  # type: ignore[arg-type]
        channels=channels,
    )


def simple_tool(name: str, fn: Any) -> ToolSpec:
    return ToolSpec(name, "test tool", {"type": "object", "properties": {}}, fn)


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[AsyncSessionStore]:
    s = AsyncSessionStore(tmp_path / "session.sqlite")
    yield s
    await s.close()


def assert_paired(items: list[dict[str, Any]]) -> None:
    seen_calls: set[str] = set()
    outputs: list[str] = []
    for item in items:
        if item.get("type") == "function_call":
            seen_calls.add(item["call_id"])
        elif item.get("type") == "function_call_output":
            assert item["call_id"] in seen_calls, "output without a preceding call"
            outputs.append(item["call_id"])
    assert len(outputs) == len(set(outputs)), "duplicate outputs"
    assert seen_calls == set(outputs), "call without output"


def output_of(items: list[dict[str, Any]], call_id: str) -> str:
    matches = [
        i for i in items if i.get("type") == "function_call_output" and i["call_id"] == call_id
    ]
    assert len(matches) == 1
    return str(matches[0]["output"])


async def rows(store: AsyncSessionStore) -> list[Item]:
    return await store.load(SID)


async def test_plain_text_is_stored_but_never_sent(store: AsyncSessionStore) -> None:
    sender = RecordingSender()
    provider = ScriptedProvider(text_resp("hello there"))
    runner = make_runner(store, provider, sender)

    result = await runner.run_turn(turn())

    assert result.status == "completed"
    assert result.tool_called is False
    assert result.replies_sent == 0
    assert sender.sent == []
    assistant = [i for i in await rows(store) if i.kind == "assistant"]
    assert len(assistant) == 1
    assert assistant[0].payload["internal"] is True
    assert assistant[0].payload["item"]["content"][0]["text"] == "hello there"


async def test_reply_defaults_to_source_channel_and_no_quote(store: AsyncSessionStore) -> None:
    sender = RecordingSender()
    provider = ScriptedProvider(
        calls_resp(("c1", "reply", {"text": "pong"})),
        text_resp("done"),
    )
    runner = make_runner(store, provider, sender)

    result = await runner.run_turn(turn())

    assert result.status == "completed"
    assert result.replies_sent == 1
    assert result.tool_called is True
    [msg] = sender.sent
    assert msg.scope == "private"
    assert msg.target_id == SRC.account
    assert msg.reply_to is None
    assert msg.segments[0].type == "text"
    assert msg.segments[0].data == {"text": "pong"}


async def test_reply_quote_true_quotes_source_message(store: AsyncSessionStore) -> None:
    sender = RecordingSender()
    provider = ScriptedProvider(
        calls_resp(("c1", "reply", {"text": "a", "quote": True})),
        text_resp("done"),
    )
    runner = make_runner(store, provider, sender)

    await runner.run_turn(turn())

    assert sender.sent[0].reply_to == IN_MSG


async def test_reply_message_id_must_be_source_message(store: AsyncSessionStore) -> None:
    sender = RecordingSender()
    provider = ScriptedProvider(
        calls_resp(("c1", "reply", {"text": "a", "message_id": "someone-else"})),
        calls_resp(("c2", "reply", {"text": "b", "message_id": IN_MSG})),
        text_resp("done"),
    )
    runner = make_runner(store, provider, sender)

    result = await runner.run_turn(turn())

    assert result.replies_sent == 1
    assert len(sender.sent) == 1
    assert sender.sent[0].reply_to == IN_MSG
    assert "source message" in output_of(provider.requests[1].input, "c1")
    errors = [
        i for i in await rows(store) if i.kind == "tool_result" and i.payload["call_id"] == "c1"
    ]
    assert errors[0].payload["is_error"] is True


async def test_quote_without_source_message_is_refused(store: AsyncSessionStore) -> None:
    sender = RecordingSender()
    provider = ScriptedProvider(
        calls_resp(("c1", "reply", {"text": "a", "quote": True})),
        text_resp("done"),
    )
    runner = make_runner(store, provider, sender)

    await runner.run_turn(turn(message_id=None))

    assert sender.sent == []
    assert "no source message" in output_of(provider.requests[1].input, "c1")


async def test_multiple_replies_in_one_turn_each_send(store: AsyncSessionStore) -> None:
    sender = RecordingSender()
    provider = ScriptedProvider(
        calls_resp(("c1", "reply", {"text": "first"}), ("c2", "reply", {"text": "second"})),
        text_resp("done"),
    )
    runner = make_runner(store, provider, sender)

    result = await runner.run_turn(turn())

    assert result.replies_sent == 2
    assert [m.segments[0].data["text"] for m in sender.sent] == ["first", "second"]


async def test_failed_send_is_error_result_visible_to_model(store: AsyncSessionStore) -> None:
    sender = RecordingSender(ok=False)
    provider = ScriptedProvider(
        calls_resp(("c1", "reply", {"text": "x"})),
        text_resp("done"),
    )
    runner = make_runner(store, provider, sender)

    result = await runner.run_turn(turn())

    assert result.status == "completed"
    assert result.replies_sent == 0
    assert "send failed" in output_of(provider.requests[1].input, "c1")


async def test_tool_errors_are_results_and_loop_continues(store: AsyncSessionStore) -> None:
    async def boom(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        raise RuntimeError("kaput")

    registry = builtin_tools()
    registry.register(simple_tool("boom", boom))
    bad_json = FunctionCall(
        call_id="c3",
        name="reply",
        arguments="{",
        parsed_arguments=None,
        parse_error="Expecting value",
        raw={"type": "function_call", "call_id": "c3", "name": "reply", "arguments": "{"},
    )
    provider = ScriptedProvider(
        calls_resp(("c1", "nope", {}), ("c2", "boom", {})),
        ModelResponse(items=(bad_json,), usage=Usage(), response_id="r", status="completed"),
        text_resp("done"),
    )
    runner = make_runner(store, provider, RecordingSender(), registry)

    result = await runner.run_turn(turn())

    assert result.status == "completed"
    second = provider.requests[1].input
    assert "unknown tool" in output_of(second, "c1")
    assert "tool failed" in output_of(second, "c2")
    assert "invalid tool arguments" in output_of(provider.requests[2].input, "c3")
    errors = [i for i in await rows(store) if i.kind == "tool_result"]
    assert all(i.payload["is_error"] for i in errors)


async def test_identity_cannot_be_overridden_by_model_args(store: AsyncSessionStore) -> None:
    sender = RecordingSender()
    provider = ScriptedProvider(
        calls_resp(("c1", "reply", {"text": "x", "user_id": "u2"})),
        calls_resp(("c2", "reply", {"text": "y", "channel": "qq:99999"})),
        text_resp("done"),
    )
    runner = make_runner(store, provider, sender)

    result = await runner.run_turn(turn())

    assert sender.sent == []
    assert result.replies_sent == 0
    assert "unknown argument" in output_of(provider.requests[1].input, "c1")
    assert "unknown channel" in output_of(provider.requests[2].input, "c2")


async def test_channel_choice_limited_to_user_channels(store: AsyncSessionStore) -> None:
    sender = RecordingSender()
    provider = ScriptedProvider(
        calls_resp(("c1", "reply", {"text": "to other", "channel": "qq:20002"})),
        text_resp("done"),
    )
    runner = make_runner(store, provider, sender)

    await runner.run_turn(turn(channels=(OTHER,)))

    assert sender.sent[0].target_id == OTHER.account
    enum = provider.requests[0].tools[0]["parameters"]["properties"]["channel"]["enum"]
    assert enum == ["qq:10001", "qq:20002"]


async def test_provider_failure_is_failed_turn_with_system_event(
    store: AsyncSessionStore,
) -> None:
    sender = RecordingSender()
    provider = ScriptedProvider(UpstreamUnavailable("down", status=503, body=""))
    runner = make_runner(store, provider, sender)

    result = await runner.run_turn(turn())

    assert result.status == "failed"
    assert result.error is not None and "UpstreamUnavailable" in result.error
    assert sender.sent == []
    events = [i for i in await rows(store) if i.kind == "system_event"]
    assert len(events) == 1
    assert events[0].payload["event"] == "model_failed"
    assert events[0].payload["status"] == 503
    assert "item" not in events[0].payload


async def test_provider_error_type_is_provider_error(store: AsyncSessionStore) -> None:
    provider = ScriptedProvider(ProviderError("bad", status=400, body=""))
    runner = make_runner(store, provider, RecordingSender())

    result = await runner.run_turn(turn())

    assert result.status == "failed"


async def test_reasoning_and_other_outputs_pass_through(store: AsyncSessionStore) -> None:
    reasoning_raw = {
        "type": "reasoning",
        "id": "rs_1",
        "summary": [],
        "status": "completed",
        "encrypted_content": "x",
    }
    other_raw = {"type": "web_search_call", "id": "ws_1"}
    first = ModelResponse(
        items=(
            Reasoning(summary="", raw=reasoning_raw),
            OtherOutput(type="web_search_call", raw=other_raw),
            FunctionCall(
                call_id="c1",
                name="reply",
                arguments=json.dumps({"text": "x"}),
                parsed_arguments={"text": "x"},
                parse_error=None,
                raw={"type": "function_call", "call_id": "c1", "name": "reply"},
            ),
        ),
        usage=Usage(),
        response_id="r",
        status="completed",
    )
    provider = ScriptedProvider(first, text_resp("done"))
    runner = make_runner(store, provider, RecordingSender())

    await runner.run_turn(turn())

    second = provider.requests[1].input
    assert second[1] == {
        "type": "reasoning",
        "summary": [],
        "encrypted_content": "x",
    }
    assert second[2] == {"type": "web_search_call"}
    assert_paired(second)


async def test_cancel_mid_tool_settles_history(store: AsyncSessionStore) -> None:
    started = asyncio.Event()

    async def slow(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        started.set()
        await asyncio.Event().wait()
        return ToolResult("never")

    registry = builtin_tools()
    registry.register(simple_tool("slow", slow))
    provider = ScriptedProvider(calls_resp(("c1", "slow", {})), text_resp("after cancel"))
    runner = make_runner(store, provider, RecordingSender(), registry)

    task = asyncio.create_task(runner.run_turn(turn()))
    await started.wait()
    assert runner.cancel(SID) is True
    result = await task

    assert result.status == "cancelled"
    results = [i for i in await rows(store) if i.kind == "tool_result"]
    assert len(results) == 1
    assert results[0].payload["output"] == CANCELLED_OUTPUT
    assert results[0].payload["is_error"] is True

    provider2 = ScriptedProvider(text_resp("ok"))
    runner2 = make_runner(store, provider2, RecordingSender(), registry)
    await runner2.run_turn(turn("again"))
    assert_paired(provider2.requests[0].input)
    assert output_of(provider2.requests[0].input, "c1") == CANCELLED_OUTPUT


async def test_cancel_without_running_turn_is_false(store: AsyncSessionStore) -> None:
    runner = make_runner(store, ScriptedProvider(), RecordingSender())
    assert runner.cancel(SID) is False
    assert runner.is_running(SID) is False


async def test_inject_lands_before_next_request(store: AsyncSessionStore) -> None:
    injected: list[bool] = []

    async def steer(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        injected.append(runner.inject(SID, [user_message("extra")]))
        return ToolResult("ok")

    registry = builtin_tools()
    registry.register(simple_tool("steer", steer))
    provider = ScriptedProvider(calls_resp(("c1", "steer", {})), text_resp("done"))
    runner = make_runner(store, provider, RecordingSender(), registry)

    result = await runner.run_turn(turn())

    assert injected == [True]
    assert result.status == "completed"
    second = provider.requests[1].input
    assert second[-1] == user_message("extra")
    assert_paired(second)


async def test_inject_without_running_turn_returns_false(store: AsyncSessionStore) -> None:
    runner = make_runner(store, ScriptedProvider(), RecordingSender())
    assert runner.inject(SID, [user_message("x")]) is False


async def test_inject_after_text_only_response_is_not_dropped(store: AsyncSessionStore) -> None:
    holder: dict[str, AgentRunner] = {}

    class InjectingProvider(ScriptedProvider):
        async def respond(self, request: ModelRequest) -> ModelResponse:
            if len(self.requests) == 0:
                holder["r"].inject(SID, [user_message("late")])
            return await super().respond(request)

    provider = InjectingProvider(text_resp("first"), text_resp("second"))
    runner = make_runner(store, provider, RecordingSender())
    holder["r"] = runner

    result = await runner.run_turn(turn())

    assert result.status == "completed"
    assert len(provider.requests) == 2
    assert provider.requests[1].input[-1] == user_message("late")


async def test_tool_called_flag_and_running_state(store: AsyncSessionStore) -> None:
    seen: list[tuple[bool, bool]] = []
    release = asyncio.Event()
    started = asyncio.Event()

    async def wait_tool(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        seen.append((runner.is_running(SID), runner.tool_called(SID)))
        started.set()
        await release.wait()
        return ToolResult("ok")

    registry = builtin_tools()
    registry.register(simple_tool("wait", wait_tool))
    provider = ScriptedProvider(calls_resp(("c1", "wait", {})), text_resp("done"))
    runner = make_runner(store, provider, RecordingSender(), registry)
    assert runner.tool_called(SID) is False

    task = asyncio.create_task(runner.run_turn(turn()))
    await started.wait()
    release.set()
    result = await task

    assert seen == [(True, True)]
    assert result.tool_called is True
    assert runner.is_running(SID) is False
    assert runner.tool_called(SID) is False


async def test_second_turn_for_same_session_is_rejected(store: AsyncSessionStore) -> None:
    release = asyncio.Event()
    started = asyncio.Event()

    async def hold(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        started.set()
        await release.wait()
        return ToolResult("ok")

    registry = builtin_tools()
    registry.register(simple_tool("hold", hold))
    provider = ScriptedProvider(calls_resp(("c1", "hold", {})), text_resp("done"))
    runner = make_runner(store, provider, RecordingSender(), registry)

    first = asyncio.create_task(runner.run_turn(turn()))
    await started.wait()
    with pytest.raises(TurnInProgressError):
        await runner.run_turn(turn("second"))
    release.set()
    assert (await first).status == "completed"


async def test_system_event_turn_is_stored_as_system_event(store: AsyncSessionStore) -> None:
    provider = ScriptedProvider(text_resp("noted"))
    runner = make_runner(store, provider, RecordingSender())

    result = await runner.run_turn(turn("[report] done", reason="system_event", message_id=None))

    assert result.status == "completed"
    kinds = [i.kind for i in await rows(store)]
    assert kinds == ["system_event", "assistant"]
    assert provider.requests[0].input == [user_message("[report] done")]


async def test_history_rebuilds_after_reopen_with_compaction(tmp_path: Path) -> None:
    path = tmp_path / "session.sqlite"
    store1 = AsyncSessionStore(path)
    provider1 = ScriptedProvider(calls_resp(("c1", "reply", {"text": "hi"})), text_resp("done"))
    runner1 = make_runner(store1, provider1, RecordingSender())
    await runner1.run_turn(turn("first"))
    await store1.close()

    store2 = AsyncSessionStore(path)
    await store2.append(SID, "compaction", {"summary": "earlier talk"})
    provider2 = ScriptedProvider(text_resp("again"))
    runner2 = make_runner(store2, provider2, RecordingSender())
    await runner2.run_turn(turn("second"))
    await store2.close()

    assert provider2.requests[0].input == [
        user_message(COMPACTION_PREFIX + "earlier talk"),
        user_message("second"),
    ]


async def test_history_rebuild_keeps_full_pairing_after_reopen(tmp_path: Path) -> None:
    path = tmp_path / "session.sqlite"
    store1 = AsyncSessionStore(path)
    provider1 = ScriptedProvider(calls_resp(("c1", "reply", {"text": "hi"})), text_resp("done"))
    runner1 = make_runner(store1, provider1, RecordingSender())
    await runner1.run_turn(turn("first"))
    await store1.close()

    store2 = AsyncSessionStore(path)
    provider2 = ScriptedProvider(text_resp("again"))
    runner2 = make_runner(store2, provider2, RecordingSender())
    await runner2.run_turn(turn("second"))
    await store2.close()

    second_input = provider2.requests[0].input
    assert second_input[0] == user_message("first")
    assert second_input[1] == {
        "type": "function_call",
        "call_id": "c1",
        "name": "reply",
        "arguments": json.dumps({"text": "hi"}),
    }
    assert second_input[2]["type"] == "function_call_output"
    assert second_input[3] == {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "done"}],
    }
    assert second_input[4] == user_message("second")
    assert_paired(second_input)


async def test_unanswered_call_from_crash_is_closed_before_next_request(
    store: AsyncSessionStore,
) -> None:
    await store.append(
        SID,
        "tool_call",
        {
            "call_id": "x1",
            "name": "reply",
            "arguments": "{}",
            "item": {"type": "function_call", "call_id": "x1", "name": "reply", "arguments": "{}"},
        },
    )
    provider = ScriptedProvider(text_resp("ok"))
    runner = make_runner(store, provider, RecordingSender())

    await runner.run_turn(turn("next"))

    input_items = provider.requests[0].input
    assert_paired(input_items)
    assert output_of(input_items, "x1") == INTERRUPTED_OUTPUT


async def test_outer_cancellation_propagates(store: AsyncSessionStore) -> None:
    started = asyncio.Event()

    async def hang(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        started.set()
        await asyncio.Event().wait()
        return ToolResult("never")

    registry = builtin_tools()
    registry.register(simple_tool("hang", hang))
    provider = ScriptedProvider(calls_resp(("c1", "hang", {})))
    runner = make_runner(store, provider, RecordingSender(), registry)

    task = asyncio.create_task(runner.run_turn(turn()))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runner.is_running(SID) is False
    results = [i for i in await rows(store) if i.kind == "tool_result"]
    assert results[0].payload["output"] == CANCELLED_OUTPUT


class GatedUserStore(AsyncSessionStore):
    """Holds the first user-item append until released, so a cancel can land mid-persist."""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def append(self, session_id: str, kind: Kind, payload: dict[str, Any]) -> int:
        if kind == "user" and not self.entered.is_set():
            self.entered.set()
            await self.release.wait()
        return await super().append(session_id, kind, payload)


class HangFirstProvider(ScriptedProvider):
    """The first model call blocks until cancelled; later calls answer from the script."""

    def __init__(self, started: asyncio.Event, *script: ModelResponse) -> None:
        super().__init__(*script)
        self._started = started

    async def respond(self, request: ModelRequest) -> ModelResponse:
        if not self.requests:
            self.requests.append(request)
            self._started.set()
            await asyncio.Event().wait()
        return await super().respond(request)


async def test_cancel_while_persisting_skips_model_and_keeps_item(tmp_path: Path) -> None:
    store = GatedUserStore(tmp_path / "session.sqlite")
    try:
        provider = ScriptedProvider(text_resp("never"))
        runner = make_runner(store, provider, RecordingSender())
        task = asyncio.create_task(runner.run_turn(turn("first")))
        await store.entered.wait()
        assert runner.is_running(SID) is True
        assert runner.cancel(SID) is True
        store.release.set()

        result = await task

        assert result.status == "cancelled"
        assert provider.requests == []
        users = [i.payload["item"] for i in await rows(store) if i.kind == "user"]
        assert users == [user_message("first")]
    finally:
        await store.close()


async def test_interrupted_turn_restarts_with_only_new_items(store: AsyncSessionStore) -> None:
    started = asyncio.Event()
    provider = HangFirstProvider(started, text_resp("reply"))
    runner = make_runner(store, provider, RecordingSender())

    task = asyncio.create_task(runner.run_turn(turn("first", message_id="m1")))
    await started.wait()
    assert runner.cancel(SID) is True
    assert (await task).status == "cancelled"

    result = await runner.run_turn(turn("second", message_id="m2"))

    assert result.status == "completed"
    sent_input = provider.requests[1].input
    assert sent_input.count(user_message("first")) == 1
    assert sent_input.count(user_message("second")) == 1
    stored = [
        (i.payload["message_id"], i.payload["item"]) for i in await rows(store) if i.kind == "user"
    ]
    assert stored == [("m1", user_message("first")), ("m2", user_message("second"))]


async def test_inject_records_message_id(store: AsyncSessionStore) -> None:
    async def steer(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        assert runner.inject(SID, [user_message("extra")], message_id="m-extra") is True
        return ToolResult("ok")

    registry = builtin_tools()
    registry.register(simple_tool("steer", steer))
    provider = ScriptedProvider(calls_resp(("c1", "steer", {})), text_resp("done"))
    runner = make_runner(store, provider, RecordingSender(), registry)

    await runner.run_turn(turn())

    injected = [
        i.payload
        for i in await rows(store)
        if i.kind == "user" and i.payload["item"] == user_message("extra")
    ]
    assert [p["message_id"] for p in injected] == ["m-extra"]


async def test_new_message_ids_are_stored_per_item(store: AsyncSessionStore) -> None:
    runner = make_runner(store, ScriptedProvider(text_resp("ok")), RecordingSender())
    ctx = TurnContext(
        user_id=USER,
        session_id=SID,
        source_channel=SRC,
        source_message_id="m2",
        new_items=[user_message("a"), user_message("b")],
        reason="user",
        new_message_ids=("m1", "m2"),
    )

    await runner.run_turn(ctx)

    stored = [
        (i.payload["message_id"], i.payload["item"]) for i in await rows(store) if i.kind == "user"
    ]
    assert stored == [("m1", user_message("a")), ("m2", user_message("b"))]


async def test_new_message_ids_must_match_items(store: AsyncSessionStore) -> None:
    runner = make_runner(store, ScriptedProvider(), RecordingSender())
    ctx = TurnContext(
        user_id=USER,
        session_id=SID,
        source_channel=SRC,
        source_message_id="m1",
        new_items=[user_message("a")],
        reason="user",
        new_message_ids=("m1", "m2"),
    )

    with pytest.raises(ValueError):
        await runner.run_turn(ctx)
    assert await rows(store) == []
