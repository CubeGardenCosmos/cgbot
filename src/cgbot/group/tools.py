"""Group-only tools for a group session turn (R3.4).

The reply tool is bound to one group id at construction and refuses any call whose
``ToolContext.session_id`` is not that group's ``group:<gid>`` session. It has no channel or
target argument, so the model cannot address a private chat or another group. ``at_sender``
only ever mentions the member who triggered the turn (taken from the event, not the model).
"""

from __future__ import annotations

import re
from typing import Any

from cgbot.agent import ToolContext, ToolRegistry, ToolResult, ToolSpec
from cgbot.model import OutboundMessage, Segment

GROUP_REPLY_TOOL_NAME = "reply"
END_CONVERSATION_TOOL_NAME = "end_conversation"

_GROUP_ID = re.compile(r"\d+")


def group_session_id(group_id: str) -> str:
    if not _GROUP_ID.fullmatch(group_id):
        raise ValueError(f"invalid group id: {group_id!r}")
    return f"group:{group_id}"


class TurnControl:
    """Per-session end flag shared by group tools and the session manager."""

    def __init__(self) -> None:
        self._ending = False

    @property
    def ending(self) -> bool:
        return self._ending

    def request_end(self) -> None:
        self._ending = True


def _refuse(message: str) -> ToolResult:
    return ToolResult(output=message, is_error=True)


def _check_args(args: dict[str, Any], allowed: frozenset[str]) -> str | None:
    unknown = sorted(set(args) - allowed)
    if unknown:
        return f"unknown argument(s): {', '.join(unknown)}"
    return None


def group_reply_tool(group_id: str, control: TurnControl) -> ToolSpec:
    allowed = frozenset({"text", "quote", "at_sender"})
    target_session = group_session_id(group_id)

    async def handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        if ctx.session_id != target_session:
            return _refuse("this reply tool only sends to its bound group")
        bad = _check_args(args, allowed)
        if bad is not None:
            return _refuse(bad)
        if control.ending:
            return _refuse("the conversation has ended in this turn; no more messages can be sent")
        text = args.get("text")
        if not isinstance(text, str) or not text.strip():
            return _refuse("text must be a non-empty string")
        quote = args.get("quote", False)
        at_sender = args.get("at_sender", False)
        if not isinstance(quote, bool) or not isinstance(at_sender, bool):
            return _refuse("quote and at_sender must be booleans")
        segments: list[Segment] = []
        if at_sender:
            if not ctx.user_id:
                return _refuse("no triggering member to @ in this turn")
            segments.append(Segment("at", {"qq": ctx.user_id}))
        segments.append(Segment("text", {"text": text}))
        reply_to: str | None = None
        if quote:
            if ctx.source_message_id is None:
                return _refuse("no source message to quote in this turn")
            reply_to = ctx.source_message_id
        result = await ctx.sender(
            OutboundMessage(
                scope="group",
                target_id=group_id,
                segments=tuple(segments),
                reply_to=reply_to,
            )
        )
        if not result.ok:
            return _refuse(f"send failed: {result.error}")
        return ToolResult(output="sent")

    return ToolSpec(
        name=GROUP_REPLY_TOOL_NAME,
        description="在本群发送一条消息。只能发到当前群。普通文本输出不会被发送。",
        parameters_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要发送到本群的文本"},
                "quote": {"type": "boolean", "description": "是否引用触发本轮的消息"},
                "at_sender": {"type": "boolean", "description": "是否 @ 触发本轮的成员"},
            },
            "required": ["text"],
            "additionalProperties": False,
        },
        handler=handler,
    )


def end_conversation_tool(group_id: str, control: TurnControl) -> ToolSpec:
    target_session = group_session_id(group_id)

    async def handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        if ctx.session_id != target_session:
            return _refuse("this tool only ends its bound group conversation")
        bad = _check_args(args, frozenset())
        if bad is not None:
            return _refuse(bad)
        control.request_end()
        return ToolResult(
            output="conversation ended; no further messages will be sent in this turn"
        )

    return ToolSpec(
        name=END_CONVERSATION_TOOL_NAME,
        description="结束当前群聊对话。调用后本轮不再发送任何消息,会话随即结束并总结。",
        parameters_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=handler,
    )


def group_tools(group_id: str, control: TurnControl) -> ToolRegistry:
    """The only tools a group turn sees: bound reply and end-conversation."""
    registry = ToolRegistry()
    registry.register(group_reply_tool(group_id, control))
    registry.register(end_conversation_tool(group_id, control))
    return registry
