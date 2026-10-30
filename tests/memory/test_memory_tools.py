"""Memory tools: identity from ToolContext, private-only, no widened arguments (R2.5-U01, U03)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, cast

import pytest

from cgbot.agent.tools import ToolContext, ToolResult, ToolSpec
from cgbot.memory import MemoryBook, Viewer, memory_tools
from cgbot.model import Channel


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


async def _no_send(_message: object) -> object:
    raise AssertionError("memory tools must not send messages")


def _ctx(session_id: str = "private:u1", user_id: str = "u1") -> ToolContext:
    return ToolContext(
        user_id=user_id,
        session_id=session_id,
        source_channel=Channel("qq", "10001"),
        source_message_id="m-1",
        sender=cast(Any, _no_send),
    )


@pytest.fixture
async def book(tmp_path) -> AsyncIterator[MemoryBook]:
    b = MemoryBook(tmp_path / "memory.sqlite", clock=Clock(), use_fts=False)
    yield b
    await b.close()


def _tools(book: MemoryBook) -> dict[str, ToolSpec]:
    return {spec.name: spec for spec in memory_tools(book)}


async def _call(spec: ToolSpec, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    return await spec.handler(ctx, args)


async def test_add_stores_private_entry_for_the_calling_user(book: MemoryBook) -> None:
    tools = _tools(book)
    result = await _call(tools["memory_add"], _ctx(), {"text": "likes tea", "tags": ["drink"]})
    assert result.is_error is False
    assert result.output.startswith("saved memory #")
    entry_id = int(result.output.removeprefix("saved memory #"))
    entry = await book.get(entry_id, viewer=Viewer.private("u1"))
    assert entry is not None
    assert entry.subject_user == "u1"
    assert entry.domain == entry.source_domain == "private"
    assert entry.tags == ("drink",)
    assert entry.importance == 3


async def test_tools_refuse_non_private_sessions(book: MemoryBook) -> None:
    tools = _tools(book)
    for spec in tools.values():
        args: dict[str, Any] = {"text": "x"} if spec.name == "memory_add" else {"query": "x"}
        if spec.name == "memory_forget":
            args = {"id": 1}
        group = await _call(spec, _ctx(session_id="group:g1"), args)
        assert group.is_error is True
        mismatch = await _call(spec, _ctx(session_id="private:u2", user_id="u1"), args)
        assert mismatch.is_error is True


async def test_tools_reject_unknown_arguments(book: MemoryBook) -> None:
    tools = _tools(book)
    widened = [
        {"text": "x", "user_id": "u2"},
        {"text": "x", "subject_user": "u2"},
        {"text": "x", "domain": "group:g1"},
        {"text": "x", "source_domain": "group:g1"},
        {"text": "x", "id": 7},
    ]
    for args in widened:
        result = await _call(tools["memory_add"], _ctx(), args)
        assert result.is_error is True, args
    search = await _call(tools["memory_search"], _ctx(), {"query": "x", "user_id": "u2"})
    assert search.is_error is True
    forget = await _call(tools["memory_forget"], _ctx(), {"id": 1, "domain": "group:g1"})
    assert forget.is_error is True
    assert await book.search("x", viewer=Viewer.private("u1"), limit=5) == []


async def test_add_validates_its_arguments(book: MemoryBook) -> None:
    add = _tools(book)["memory_add"]
    cases = [
        {},
        {"text": "   "},
        {"text": 123},
        {"text": "ok", "tags": "not-a-list"},
        {"text": "ok", "importance": True},
        {"text": "ok", "importance": 9},
        {"text": "ok", "tags": ["has space"]},
    ]
    for args in cases:
        assert (await _call(add, _ctx(), args)).is_error is True, args


async def test_search_formats_visible_results_and_reports_empty(book: MemoryBook) -> None:
    tools = _tools(book)
    add = tools["memory_add"]
    await _call(add, _ctx(), {"text": "likes green tea"})
    found = await _call(tools["memory_search"], _ctx(), {"query": "tea", "limit": 3})
    assert found.is_error is False
    assert "likes green tea" in found.output
    assert found.output.startswith("- [#")
    empty = await _call(tools["memory_search"], _ctx(), {"query": "absent-term"})
    assert empty.output == "no matching memories"
    assert empty.is_error is False


async def test_search_validates_query_and_limit(book: MemoryBook) -> None:
    search = _tools(book)["memory_search"]
    assert (await _call(search, _ctx(), {})).is_error is True
    assert (await _call(search, _ctx(), {"query": "  "})).is_error is True
    assert (await _call(search, _ctx(), {"query": "x", "limit": 0})).is_error is True
    assert (await _call(search, _ctx(), {"query": "x", "limit": 11})).is_error is True
    assert (await _call(search, _ctx(), {"query": "x", "limit": True})).is_error is True


async def test_forget_deletes_own_entry_only(book: MemoryBook) -> None:
    tools = _tools(book)
    mine = await _call(tools["memory_add"], _ctx(), {"text": "mine"})
    mine_id = int(mine.output.removeprefix("saved memory #"))
    other_user_entry = await book.add(
        subject_user="u2", domain="private", source_domain="private", text="theirs"
    )

    refused = await _call(tools["memory_forget"], _ctx(), {"id": other_user_entry.id})
    assert refused.is_error is True
    assert await book.get(other_user_entry.id, viewer=Viewer.private("u2")) is not None

    removed = await _call(tools["memory_forget"], _ctx(), {"id": mine_id})
    assert removed.is_error is False
    assert await book.get(mine_id, viewer=Viewer.private("u1")) is None


async def test_forget_rejects_bad_ids(book: MemoryBook) -> None:
    forget = _tools(book)["memory_forget"]
    for bad in [{}, {"id": True}, {"id": 0}, {"id": "1"}, {"id": -3}]:
        assert (await _call(forget, _ctx(), bad)).is_error is True, bad


async def test_tool_written_entries_stay_out_of_group_views(book: MemoryBook) -> None:
    await _call(_tools(book)["memory_add"], _ctx(), {"text": "private hobby tea"})
    group = Viewer.group("g1", ["u1"])
    assert await book.search("tea", viewer=group, limit=5) == []
    assert await book.list_for_prompt(viewer=group, budget_chars=10_000) == []


def test_schemas_are_closed_and_named() -> None:
    specs = memory_tools(cast(MemoryBook, object()))
    assert [s.name for s in specs] == ["memory_add", "memory_search", "memory_forget"]
    for spec in specs:
        assert spec.parameters_schema["additionalProperties"] is False
        assert spec.channel_param is None
