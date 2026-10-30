"""The built-in ``reply`` tool (R1.6): the only way model output reaches a user.

Target and quote rules:
- The recipient is the account of the chosen channel: the source channel by default, or one of
  ``ToolContext.channels`` selected by its ``qq:<account>`` key. Never a model-supplied user id.
- ``quote=true`` quotes the source message of the turn; ``message_id`` must equal that source
  message. Any other id is refused with an error result the model sees.
"""

from __future__ import annotations

from typing import Any

from cgbot.agent.tools import (
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    available_channels,
    channel_key,
)
from cgbot.model import OutboundMessage, Segment

REPLY_TOOL_NAME = "reply"
_ALLOWED_ARGS = frozenset({"text", "quote", "message_id", "channel"})


def _error(message: str) -> ToolResult:
    return ToolResult(output=message, is_error=True)


async def _reply(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    unknown = sorted(set(args) - _ALLOWED_ARGS)
    if unknown:
        return _error(
            f"unknown argument(s): {', '.join(unknown)}; the recipient is fixed by the event"
        )

    text = args.get("text")
    if not isinstance(text, str) or not text.strip():
        return _error("text must be a non-empty string")

    quote = args.get("quote", False)
    if not isinstance(quote, bool):
        return _error("quote must be a boolean")

    message_id = args.get("message_id")
    if message_id is not None and (not isinstance(message_id, str) or not message_id):
        return _error("message_id must be a non-empty string or null")

    reply_to: str | None = None
    if message_id is not None:
        if message_id != ctx.source_message_id:
            return _error(
                "message_id is not the source message of this turn; "
                "only the source message can be quoted"
            )
        reply_to = message_id
    elif quote:
        if ctx.source_message_id is None:
            return _error("there is no source message to quote in this turn")
        reply_to = ctx.source_message_id

    channels = available_channels(ctx.source_channel, ctx.channels)
    channel_arg = args.get("channel")
    if channel_arg is None:
        target = ctx.source_channel
    else:
        matches = [c for c in channels if channel_key(c) == channel_arg]
        if not matches:
            choices = ", ".join(channel_key(c) for c in channels)
            return _error(f"unknown channel {channel_arg!r}; available: {choices}")
        target = matches[0]

    result = await ctx.sender(
        OutboundMessage(
            scope="private",
            target_id=target.account,
            segments=(Segment("text", {"text": text}),),
            reply_to=reply_to,
        )
    )
    if not result.ok:
        return _error(f"send failed: {result.error or 'unknown error'}")
    quoted = " (quoting the source message)" if reply_to is not None else ""
    return ToolResult(output=f"sent via {channel_key(target)}{quoted}")


REPLY_TOOL = ToolSpec(
    name=REPLY_TOOL_NAME,
    description=(
        "向用户发送一条消息。这是唯一能把内容发给用户的方式;普通文本输出不会发出。"
        "不调用本工具即为沉默。省略 channel 时发到来源入口;quote 为 true 时引用来源消息。"
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "要发送的文本,不能为空"},
            "quote": {
                "type": "boolean",
                "description": "为 true 时引用本轮来源消息",
                "default": False,
            },
            "message_id": {
                "type": ["string", "null"],
                "description": "要引用的消息 ID,只能是本轮来源消息的 ID",
            },
            "channel": {
                "type": "string",
                "description": "发送入口,省略则为来源入口",
            },
        },
        "required": ["text"],
        "additionalProperties": False,
    },
    handler=_reply,
    channel_param="channel",
)


def builtin_tools() -> ToolRegistry:
    """A fresh registry holding the built-in tools (currently only ``reply``)."""
    registry = ToolRegistry()
    registry.register(REPLY_TOOL)
    return registry
