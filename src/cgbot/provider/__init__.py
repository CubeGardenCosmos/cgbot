"""Single-upstream model provider (R1.5): domain types, errors, Responses adapter."""

from cgbot.provider.errors import BadRequest, ProviderError, RateLimited, UpstreamUnavailable
from cgbot.provider.items import function_call_output, item_from_output, user_message
from cgbot.provider.responses import ResponsesProvider
from cgbot.provider.types import (
    Completed,
    FunctionCall,
    ItemDone,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    OtherOutput,
    OutputItem,
    Reasoning,
    StreamEvent,
    TextDelta,
    TextOutput,
    Usage,
)

__all__ = [
    "BadRequest",
    "Completed",
    "FunctionCall",
    "ItemDone",
    "ModelProvider",
    "ModelRequest",
    "ModelResponse",
    "OtherOutput",
    "OutputItem",
    "ProviderError",
    "RateLimited",
    "Reasoning",
    "ResponsesProvider",
    "StreamEvent",
    "TextDelta",
    "TextOutput",
    "Usage",
    "UpstreamUnavailable",
    "function_call_output",
    "item_from_output",
    "user_message",
]
