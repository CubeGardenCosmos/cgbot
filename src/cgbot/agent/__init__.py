"""Agent loop (R1.6): model turns, the built-in reply tool, cancel and inject."""

from cgbot.agent.history import COMPACTION_PREFIX, to_input_items, unanswered_calls
from cgbot.agent.reply import REPLY_TOOL, REPLY_TOOL_NAME, builtin_tools
from cgbot.agent.runner import (
    AgentRunner,
    TurnContext,
    TurnInProgressError,
    TurnResult,
    TurnStatus,
)
from cgbot.agent.tools import (
    ToolContext,
    ToolHandler,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    available_channels,
    channel_key,
)

__all__ = [
    "COMPACTION_PREFIX",
    "REPLY_TOOL",
    "REPLY_TOOL_NAME",
    "AgentRunner",
    "ToolContext",
    "ToolHandler",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "TurnContext",
    "TurnInProgressError",
    "TurnResult",
    "TurnStatus",
    "available_channels",
    "builtin_tools",
    "channel_key",
    "to_input_items",
    "unanswered_calls",
]
