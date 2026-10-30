"""Model-callable memory tools (R2.5), private chat only.

Identity and domain are never taken from model arguments: the handler derives the viewer from
``ToolContext`` and only accepts calls from the private session of that user
(``session_id == "private:<user_id>"``). Writes are hardcoded to domain ``private``. Any argument
not in the schema (for example ``user_id``, ``subject_user``, ``domain``, ``source_domain``, or
``id`` on add) is rejected, so the model cannot widen scope.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from cgbot.agent.tools import ToolContext, ToolHandler, ToolResult, ToolSpec
from cgbot.memory.book import MemoryBook
from cgbot.memory.domain import PRIVATE_DOMAIN, PRIVATE_SESSION_PREFIX, Viewer, check_id
from cgbot.memory.store import (
    DEFAULT_IMPORTANCE,
    MAX_IMPORTANCE,
    MAX_TAGS,
    MIN_IMPORTANCE,
    format_entries,
)

_SEARCH_DEFAULT_LIMIT = 5
_SEARCH_MAX_LIMIT = 10


def _error(message: str) -> ToolResult:
    return ToolResult(output=message, is_error=True)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _unknown_args(args: dict[str, Any], allowed: set[str]) -> ToolResult | None:
    unknown = sorted(set(args) - allowed)
    if unknown:
        return _error(f"unknown argument(s): {', '.join(unknown)}")
    return None


def _guarded(
    allowed: set[str],
    run: Callable[[Viewer, dict[str, Any]], Awaitable[ToolResult]],
) -> ToolHandler:
    """Common preamble: private session only, valid identity, no unknown args."""

    async def handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        if ctx.session_id != f"{PRIVATE_SESSION_PREFIX}{ctx.user_id}":
            return _error("memory tools are only available in the private chat")
        try:
            viewer = Viewer.private(check_id(ctx.user_id, "user id"))
        except ValueError:
            return _error("invalid user identity")
        if (refused := _unknown_args(args, allowed)) is not None:
            return refused
        return await run(viewer, args)

    return handler


def _make_add(book: MemoryBook) -> ToolHandler:
    async def run(viewer: Viewer, args: dict[str, Any]) -> ToolResult:
        text = args.get("text")
        if not isinstance(text, str):
            return _error("text is required")
        tags = args.get("tags", [])
        if not isinstance(tags, list):
            return _error("tags must be a list of strings")
        importance = args.get("importance", DEFAULT_IMPORTANCE)
        if not _is_int(importance):
            return _error(f"importance must be an integer {MIN_IMPORTANCE}-{MAX_IMPORTANCE}")
        try:
            entry = await book.add(
                subject_user=next(iter(viewer.subjects)),
                domain=PRIVATE_DOMAIN,
                source_domain=PRIVATE_DOMAIN,
                text=text,
                tags=tags,
                importance=importance,
            )
        except ValueError as exc:
            return _error(str(exc))
        return ToolResult(output=f"saved memory #{entry.id}", is_error=False)

    return _guarded({"text", "tags", "importance"}, run)


def _make_search(book: MemoryBook) -> ToolHandler:
    async def run(viewer: Viewer, args: dict[str, Any]) -> ToolResult:
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            return _error("query is required")
        limit = args.get("limit", _SEARCH_DEFAULT_LIMIT)
        if not _is_int(limit) or not 1 <= limit <= _SEARCH_MAX_LIMIT:
            return _error(f"limit must be an integer 1-{_SEARCH_MAX_LIMIT}")
        try:
            entries = await book.search(query, viewer=viewer, limit=limit)
        except ValueError as exc:
            return _error(str(exc))
        if not entries:
            return ToolResult(output="no matching memories", is_error=False)
        return ToolResult(output=format_entries(entries), is_error=False)

    return _guarded({"query", "limit"}, run)


def _make_forget(book: MemoryBook) -> ToolHandler:
    async def run(viewer: Viewer, args: dict[str, Any]) -> ToolResult:
        entry_id = args.get("id")
        if not isinstance(entry_id, int) or isinstance(entry_id, bool) or entry_id < 1:
            return _error("id must be a positive integer")
        if not await book.delete(entry_id, viewer=viewer):
            return _error(f"no memory #{entry_id} to forget")
        return ToolResult(output=f"forgot memory #{entry_id}", is_error=False)

    return _guarded({"id"}, run)


def memory_tools(book: MemoryBook) -> tuple[ToolSpec, ...]:
    """The three private-chat memory tools bound to ``book``."""
    return (
        ToolSpec(
            name="memory_add",
            description=(
                "Save a durable fact or preference about the user from this private chat. "
                "Keep text short and self-contained."
            ),
            parameters_schema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "maxLength": 500},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string", "maxLength": 32},
                        "maxItems": MAX_TAGS,
                    },
                    "importance": {
                        "type": "integer",
                        "minimum": MIN_IMPORTANCE,
                        "maximum": MAX_IMPORTANCE,
                    },
                },
                "required": ["text"],
                "additionalProperties": False,
            },
            handler=_make_add(book),
        ),
        ToolSpec(
            name="memory_search",
            description="Search this user's saved memories in the private chat.",
            parameters_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "maxLength": 200},
                    "limit": {"type": "integer", "minimum": 1, "maximum": _SEARCH_MAX_LIMIT},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            handler=_make_search(book),
        ),
        ToolSpec(
            name="memory_forget",
            description="Delete one saved memory by its id from this private chat.",
            parameters_schema={
                "type": "object",
                "properties": {"id": {"type": "integer", "minimum": 1}},
                "required": ["id"],
                "additionalProperties": False,
            },
            handler=_make_forget(book),
        ),
    )
