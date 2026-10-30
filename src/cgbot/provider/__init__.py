"""Single-upstream model provider (R1.5): domain types, errors, Responses and Chat adapters."""

import httpx

from cgbot.config.settings import ConfigError, ProviderConfig
from cgbot.provider.chat import ChatCompletionsProvider
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


def build_provider(
    cfg: ProviderConfig, http: httpx.AsyncClient | None = None
) -> ResponsesProvider | ChatCompletionsProvider:
    """Adapter for ``cfg.wire_api``: ``responses`` (default) or ``chat``."""
    if cfg.wire_api == "responses":
        return ResponsesProvider(
            base_url=cfg.base_url,
            api_key=cfg.api_key.reveal(),
            model=cfg.model,
            context_window=cfg.context_window,
            http=http,
        )
    if cfg.wire_api == "chat":
        return ChatCompletionsProvider(
            base_url=cfg.base_url,
            api_key=cfg.api_key.reveal(),
            model=cfg.model,
            context_window=cfg.context_window,
            http=http,
        )
    raise ConfigError("provider.wire_api", f"未知值 {cfg.wire_api!r}")


__all__ = [
    "BadRequest",
    "ChatCompletionsProvider",
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
    "build_provider",
    "function_call_output",
    "item_from_output",
    "user_message",
]
