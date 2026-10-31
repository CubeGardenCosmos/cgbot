"""Model-callable ``meme_lookup`` tool, shared by private and group sessions.

Scope is derived from ``ToolContext.session_id``, never from model arguments:

* ``group:<gid>`` sees only that group's entries;
* ``private:<user_id>`` (the caller's own private session) sees entries from every group;
* any other session is refused.
"""

from __future__ import annotations

from typing import Any

from cgbot.agent.tools import ToolContext, ToolResult, ToolSpec
from cgbot.meme.store import MAX_QUERY_CHARS, MemeEntry, MemeStore
from cgbot.memory.domain import PRIVATE_SESSION_PREFIX

GROUP_SESSION_PREFIX = "group:"
MEME_LOOKUP_TOOL_NAME = "meme_lookup"

_DESCRIPTION = (
    "查询梗与黑话词条库(来自各群历次群聊总结的群内梗、黑话)。"
    "何时用:遇到可能是网络梗、黑话或群内梗、你不确定含义时,先调用本工具;"
    "查不到再考虑 web_search。群聊只能查到本群的词条,私聊可查到所有群的词条。"
    "结果只是参考:用自己的话解释,不要整段照抄,也不要把查询过程告诉用户。"
)


def _error(message: str) -> ToolResult:
    return ToolResult(output=message, is_error=True)


def _scope(ctx: ToolContext) -> tuple[bool, str | None]:
    """Return ``(allowed, group_id)``; ``group_id=None`` means all groups."""
    session_id = ctx.session_id
    if session_id.startswith(GROUP_SESSION_PREFIX):
        gid = session_id[len(GROUP_SESSION_PREFIX) :]
        return (True, gid) if gid.isdigit() else (False, None)
    if session_id == f"{PRIVATE_SESSION_PREFIX}{ctx.user_id}":
        return True, None
    return False, None


def _format(entries: tuple[MemeEntry, ...]) -> str:
    lines = []
    for entry in entries:
        lines.append(f"- {entry.term}: {entry.meaning}" if entry.meaning else f"- {entry.term}")
    return "\n".join(lines)


def meme_lookup_tool(store: MemeStore) -> ToolSpec:
    async def handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        allowed, group_id = _scope(ctx)
        if not allowed:
            return _error("meme lookup needs a group or the owner's private session")
        unknown = sorted(set(args) - {"query"})
        if unknown:
            return _error(f"unknown argument(s): {', '.join(unknown)}")
        query = args.get("query")
        if not isinstance(query, str) or not " ".join(query.split()):
            return _error("query is required and must be a non-empty string")
        if len(" ".join(query.split())) > MAX_QUERY_CHARS:
            return _error(f"query must be at most {MAX_QUERY_CHARS} characters")
        entries = await store.lookup(query, group_id=group_id)
        if not entries:
            return ToolResult(output="词条库里没有相关记录。", is_error=False)
        return ToolResult(output=_format(entries), is_error=False)

    return ToolSpec(
        name=MEME_LOOKUP_TOOL_NAME,
        description=_DESCRIPTION,
        parameters_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "maxLength": MAX_QUERY_CHARS,
                    "description": "要查的梗名、黑话原词或关键词",
                },
            },
            "required": ["query"],
        },
        handler=handler,
    )
