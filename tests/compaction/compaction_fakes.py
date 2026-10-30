from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from cgbot.provider import (
    ModelRequest,
    ModelResponse,
    StreamEvent,
    TextOutput,
    Usage,
    user_message,
)
from cgbot.session import AsyncSessionStore

SID = "private:u1"


class FakeProvider:
    """Records requests; answers with one text output, or raises ``error``."""

    def __init__(
        self,
        *,
        context_window: int = 100_000,
        summary: str = "摘要正文",
        status: str = "completed",
        error: BaseException | None = None,
        on_request: Callable[[ModelRequest], Awaitable[None]] | None = None,
        gate: asyncio.Event | None = None,
    ) -> None:
        self._context_window = context_window
        self.summary = summary
        self.status = status
        self.error = error
        self.on_request = on_request
        self.gate = gate
        self.requests: list[ModelRequest] = []

    @property
    def name(self) -> str:
        return "fake"

    @property
    def context_window(self) -> int:
        return self._context_window

    @context_window.setter
    def context_window(self, value: int) -> None:
        self._context_window = value

    async def respond(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if self.on_request is not None:
            await self.on_request(request)
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        items: tuple[TextOutput, ...] = (
            (TextOutput(text=self.summary, raw={}),) if self.summary else ()
        )
        return ModelResponse(
            items=items,
            usage=Usage(input_tokens=1),
            response_id="resp",
            status=self.status,
            incomplete_reason=None,
        )

    async def respond_stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        raise AssertionError("compaction does not stream")
        yield  # pragma: no cover


class Rows:
    """Appends rows in the layout written by ``AgentRunner``."""

    def __init__(self, store: AsyncSessionStore, session_id: str = SID) -> None:
        self.store = store
        self.sid = session_id

    async def user(self, text: str) -> int:
        return await self.store.append(self.sid, "user", {"item": user_message(text)})

    async def system(self, text: str) -> int:
        return await self.store.append(self.sid, "system_event", {"item": user_message(text)})

    async def assistant(self, text: str) -> int:
        item: dict[str, Any] = {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
        }
        return await self.store.append(self.sid, "assistant", {"item": item, "internal": True})

    async def reasoning(self, text: str) -> int:
        item: dict[str, Any] = {"type": "reasoning", "summary": [{"text": text}]}
        return await self.store.append(self.sid, "assistant", {"item": item, "internal": True})

    async def call(self, call_id: str, name: str = "lookup", arguments: str = "{}") -> int:
        item = {"type": "function_call", "call_id": call_id, "name": name, "arguments": arguments}
        return await self.store.append(
            self.sid,
            "tool_call",
            {"call_id": call_id, "name": name, "arguments": arguments, "item": item},
        )

    async def result(self, call_id: str, output: str = "ok", name: str = "lookup") -> int:
        return await self.store.append(
            self.sid,
            "tool_result",
            {"call_id": call_id, "name": name, "output": output, "is_error": False},
        )

    async def exchange(self, text: str, reply: str) -> int:
        await self.user(text)
        return await self.assistant(reply)
