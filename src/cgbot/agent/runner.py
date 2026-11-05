"""Agent loop (R1.6): one turn = model requests and tool calls until a response has no tool call.

Persistence: every model output item, tool call, tool result and input item is appended to the
session store as it happens, so the history is always the source of truth. Plain model text is
stored (kind ``assistant``, ``internal``) and never sent; only the ``reply`` tool sends.

Cancellation and injection (for R2.1/R2.2 steering):
- New items are persisted before the turn can be cancelled, so a cancelled turn never loses them;
  a caller restarting after a cancel must pass only the items not yet passed to any turn.
- ``cancel(session_id)`` cancels the running turn. If the turn is still persisting its new items,
  the request is honoured right after persistence (no model call). Tool calls persisted without a
  result receive a synthetic cancelled ``tool_result`` before the turn returns ``cancelled``.
- ``inject(session_id, items, message_id=...)`` queues input items recorded with that message id;
  they are appended before the next model request of the running turn (after the tool results
  that precede them). If the model answers without a tool call while items are queued, the loop
  continues so they are not dropped.

Images: when an ``images`` source is given, the data URLs of the images in the newest
``recent_image_turns`` image-bearing user turns are loaded (scoped by the session id) before each
request and rendered as ``input_image`` parts; older images stay as the text marker.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from cgbot.agent.history import (
    CANCELLED_OUTPUT,
    INTERRUPTED_OUTPUT,
    expand_window,
    recent_image_refs,
    render_items,
    unanswered_calls,
)
from cgbot.agent.tools import (
    ToolContext,
    ToolRegistry,
    ToolResult,
    available_channels,
    channel_key,
)
from cgbot.memory.domain import PRIVATE_SESSION_PREFIX
from cgbot.model import Channel, OutboundMessage, Segment, SendResult
from cgbot.provider import (
    FunctionCall,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ProviderError,
    Usage,
    item_from_output,
)
from cgbot.session import AsyncSessionStore, Kind

log = logging.getLogger(__name__)

Sender = Callable[[OutboundMessage], Awaitable[SendResult]]
TurnStatus = Literal["completed", "cancelled", "failed"]
ResponseObserver = Callable[[str, Usage, int], None]
InstructionsSource = str | Callable[[str], Awaitable[str]]


class ImageSource(Protocol):
    """Loads stored chat images of one session as ``ref -> data URL`` (missing refs omitted)."""

    async def load(self, session_id: str, refs: Sequence[str]) -> dict[str, str]: ...


@dataclass(frozen=True, slots=True)
class TurnContext:
    """What starts a turn. ``new_items`` are Responses input items (e.g. ``user_message``).
    ``channels`` are the user's reachable channels; the source channel is always included."""

    user_id: str
    session_id: str
    source_channel: Channel
    source_message_id: str | None
    new_items: list[dict[str, Any]]
    reason: Literal["user", "system_event"]
    channels: tuple[Channel, ...] = ()
    # One source message id per ``new_items`` entry; empty means ``source_message_id`` for all.
    new_message_ids: tuple[str | None, ...] = ()


@dataclass(frozen=True, slots=True)
class TurnResult:
    status: TurnStatus
    tool_called: bool
    replies_sent: int
    error: str | None = None


class TurnInProgressError(RuntimeError):
    """A turn is already running for this session; one turn per session at a time."""


@dataclass(eq=False)
class _Turn:
    task: asyncio.Task[TurnResult] | None = None
    tool_called: bool = False
    replies_sent: int = 0
    cancel_requested: bool = False
    # call_id -> tool name for calls persisted without a result yet, in call order.
    pending: dict[str, str] = field(default_factory=dict)
    injected: list[tuple[dict[str, Any], str | None]] = field(default_factory=list)


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


class AgentRunner:
    def __init__(
        self,
        provider: ModelProvider,
        store: AsyncSessionStore,
        tools: ToolRegistry,
        instructions: InstructionsSource,
        sender: Sender,
        reasoning_effort: str | None = None,
        on_response: ResponseObserver | None = None,
        images: ImageSource | None = None,
        recent_image_turns: int = 0,
        failure_notice: str | None = None,
    ) -> None:
        self._provider = provider
        self._store = store
        self._tools = tools
        self._instructions = instructions
        self._sender = sender
        self._reasoning_effort = reasoning_effort
        self._on_response = on_response
        self._images = images
        self._recent_image_turns = recent_image_turns
        self._failure_notice = failure_notice
        self._turns: dict[str, _Turn] = {}

    async def run_turn(self, ctx: TurnContext) -> TurnResult:
        if ctx.reason not in ("user", "system_event"):
            raise ValueError(f"unknown turn reason: {ctx.reason!r}")
        if ctx.new_message_ids and len(ctx.new_message_ids) != len(ctx.new_items):
            raise ValueError("new_message_ids must align one to one with new_items")
        if ctx.session_id in self._turns:
            raise TurnInProgressError(f"a turn is already running for session {ctx.session_id}")
        turn = _Turn()
        self._turns[ctx.session_id] = turn
        try:
            await self._persist_new_items(ctx)
            result = await self._start(ctx, turn)
        finally:
            del self._turns[ctx.session_id]
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            # The caller was cancelled while the turn settled; keep cancellation propagating.
            raise asyncio.CancelledError()
        return result

    async def _persist_new_items(self, ctx: TurnContext) -> None:
        kind: Kind = "user" if ctx.reason == "user" else "system_event"
        channel = channel_key(ctx.source_channel)
        ids = ctx.new_message_ids or (ctx.source_message_id,) * len(ctx.new_items)
        for item, message_id in zip(ctx.new_items, ids, strict=True):
            await self._append_item(
                ctx.session_id, kind, item, {"message_id": message_id, "channel": channel}
            )

    async def _start(self, ctx: TurnContext, turn: _Turn) -> TurnResult:
        if turn.cancel_requested:
            return TurnResult("cancelled", False, 0)
        task = asyncio.create_task(self._drive(ctx, turn))
        turn.task = task
        try:
            return await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            # Cancelled before the task ever ran: nothing started, so nothing to settle.
            return TurnResult("cancelled", False, 0)

    def is_running(self, session_id: str) -> bool:
        return session_id in self._turns

    def tool_called(self, session_id: str) -> bool:
        """Whether the running turn of this session has issued a tool call."""
        turn = self._turns.get(session_id)
        return turn is not None and turn.tool_called

    def cancel(self, session_id: str) -> bool:
        """Request cancellation of the running turn. False if none is running or stopping."""
        turn = self._turns.get(session_id)
        if turn is None or turn.cancel_requested:
            return False
        if turn.task is None:
            # Still persisting new items: honoured as soon as the task would start.
            turn.cancel_requested = True
            return True
        if turn.task.done():
            return False
        turn.cancel_requested = True
        turn.task.cancel()
        return True

    def inject(
        self, session_id: str, items: Sequence[dict[str, Any]], *, message_id: str | None = None
    ) -> bool:
        """Queue input items for the running turn. False if no turn is accepting injections."""
        turn = self._turns.get(session_id)
        if turn is None or turn.task is None or turn.task.done() or turn.cancel_requested:
            return False
        turn.injected.extend((copy.deepcopy(dict(item)), message_id) for item in items)
        return True

    async def _drive(self, ctx: TurnContext, turn: _Turn) -> TurnResult:
        try:
            result = await self._loop(ctx, turn)
        except asyncio.CancelledError:
            await self._settle_cancelled(ctx, turn)
            return TurnResult("cancelled", turn.tool_called, turn.replies_sent)
        except Exception as exc:
            log.warning("turn failed session=%s error=%s", ctx.session_id, type(exc).__name__)
            await self._flush_injected(ctx, turn)
            return TurnResult("failed", turn.tool_called, turn.replies_sent, _describe(exc))
        if result.status != "completed":
            await self._flush_injected(ctx, turn)
        return result

    async def _loop(self, ctx: TurnContext, turn: _Turn) -> TurnResult:
        channels = available_channels(ctx.source_channel, ctx.channels)
        await self._repair_unanswered(ctx.session_id)

        while True:
            await self._flush_injected(ctx, turn)
            items = await self._store.load_active(ctx.session_id)
            instructions = (
                self._instructions
                if isinstance(self._instructions, str)
                else await self._instructions(ctx.session_id)
            )
            expanded = expand_window(items)
            images: dict[str, str] = {}
            if self._images is not None:
                refs = recent_image_refs(expanded, self._recent_image_turns)
                if refs:
                    images = await self._images.load(ctx.session_id, refs)
            request = ModelRequest(
                instructions=instructions,
                input=render_items(expanded, images),
                tools=self._tools.schemas(channels),
                reasoning_effort=self._reasoning_effort,
            )
            try:
                response = await self._provider.respond(request)
            except Exception as exc:
                await self._record_model_failure(ctx, turn, exc)
                return TurnResult("failed", turn.tool_called, turn.replies_sent, _describe(exc))

            if self._on_response is not None:
                self._on_response(ctx.session_id, response.usage, items[-1].seq if items else 0)
            calls = await self._persist_response(ctx, turn, response)
            if not calls:
                if turn.injected:
                    continue
                return TurnResult("completed", turn.tool_called, turn.replies_sent)

            for call in calls:
                result = await self._run_tool(ctx, turn, call, channels)
                await self._append(
                    ctx.session_id,
                    "tool_result",
                    {
                        "call_id": call.call_id,
                        "name": call.name,
                        "output": result.output,
                        "is_error": result.is_error,
                    },
                )
                turn.pending.pop(call.call_id, None)

    async def _persist_response(
        self, ctx: TurnContext, turn: _Turn, response: ModelResponse
    ) -> list[FunctionCall]:
        calls: list[FunctionCall] = []
        for output in response.items:
            if isinstance(output, FunctionCall):
                await self._append(
                    ctx.session_id,
                    "tool_call",
                    {
                        "call_id": output.call_id,
                        "name": output.name,
                        "arguments": output.arguments,
                        "item": item_from_output(output),
                    },
                )
                turn.tool_called = True
                turn.pending[output.call_id] = output.name
                calls.append(output)
            else:
                await self._append(
                    ctx.session_id,
                    "assistant",
                    {"item": item_from_output(output), "internal": True},
                )
        return calls

    async def _run_tool(
        self, ctx: TurnContext, turn: _Turn, call: FunctionCall, channels: tuple[Channel, ...]
    ) -> ToolResult:
        if call.parsed_arguments is None:
            return ToolResult(f"invalid tool arguments: {call.parse_error}", is_error=True)
        spec = self._tools.get(call.name)
        if spec is None:
            return ToolResult(f"unknown tool: {call.name}", is_error=True)
        tool_ctx = ToolContext(
            user_id=ctx.user_id,
            session_id=ctx.session_id,
            source_channel=ctx.source_channel,
            source_message_id=ctx.source_message_id,
            sender=self._counting_sender(turn),
            channels=channels,
        )
        try:
            return await spec.handler(tool_ctx, dict(call.parsed_arguments))
        except Exception as exc:
            return ToolResult(f"tool failed: {_describe(exc)}", is_error=True)

    def _counting_sender(self, turn: _Turn) -> Sender:
        async def send(message: OutboundMessage) -> SendResult:
            result = await self._sender(message)
            if result.ok:
                turn.replies_sent += 1
            return result

        return send

    async def _settle_cancelled(self, ctx: TurnContext, turn: _Turn) -> None:
        for call_id, name in list(turn.pending.items()):
            await self._append(
                ctx.session_id,
                "tool_result",
                {"call_id": call_id, "name": name, "output": CANCELLED_OUTPUT, "is_error": True},
            )
        turn.pending.clear()
        await self._flush_injected(ctx, turn)

    async def _repair_unanswered(self, session_id: str) -> None:
        """Close calls left unanswered by an earlier process (crash mid-tool)."""
        items = await self._store.load_active(session_id)
        for call_id, name in unanswered_calls(items):
            await self._append(
                session_id,
                "tool_result",
                {"call_id": call_id, "name": name, "output": INTERRUPTED_OUTPUT, "is_error": True},
            )

    async def _flush_injected(self, ctx: TurnContext, turn: _Turn) -> None:
        # Index-based so items queued during an await are picked up; each is removed only
        # after it is persisted, so a cancellation mid-way cannot drop it.
        while turn.injected:
            item, message_id = turn.injected[0]
            await self._append_item(ctx.session_id, "user", item, {"message_id": message_id})
            turn.injected.pop(0)

    async def _record_model_failure(self, ctx: TurnContext, turn: _Turn, exc: Exception) -> None:
        status = exc.status if isinstance(exc, ProviderError) else None
        log.warning(
            "model request failed session=%s status=%s replies_sent=%d detail=%s",
            ctx.session_id,
            status,
            turn.replies_sent,
            _describe(exc),
        )
        await self._append(
            ctx.session_id,
            "system_event",
            {
                "event": "model_failed",
                "error": type(exc).__name__,
                "detail": str(exc),
                "status": status,
            },
        )
        if (
            self._failure_notice is not None
            and ctx.session_id.startswith(PRIVATE_SESSION_PREFIX)
            and turn.replies_sent == 0
        ):
            await self._send_failure_notice(ctx)

    async def _send_failure_notice(self, ctx: TurnContext) -> None:
        """Tell the user in private that the turn got no reply. Fixed text, never via the model;
        recorded as an input-less ``system_event`` so history replay does not show it as the
        assistant's words."""
        assert self._failure_notice is not None
        error: str | None = None
        try:
            result = await self._sender(
                OutboundMessage(
                    scope="private",
                    target_id=ctx.source_channel.account,
                    segments=(Segment("text", {"text": self._failure_notice}),),
                )
            )
            if not result.ok:
                error = result.error or "send failed"
        except Exception as exc:
            error = _describe(exc)
        if error is not None:
            log.warning(
                "model failure notice not delivered session=%s error=%s", ctx.session_id, error
            )
        await self._append(
            ctx.session_id,
            "system_event",
            {"event": "model_failure_notice", "text": self._failure_notice, "sent": error is None},
        )

    async def _append_item(
        self, session_id: str, kind: Kind, item: dict[str, Any], meta: dict[str, Any]
    ) -> None:
        await self._append(session_id, kind, {**meta, "item": copy.deepcopy(dict(item))})

    async def _append(self, session_id: str, kind: Kind, payload: dict[str, Any]) -> None:
        await self._store.append(session_id, kind, payload)
