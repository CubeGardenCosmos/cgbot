"""Model provider (R1.5): domain types, errors, Responses/Chat/Gemini adapters, ordered fallback."""

import httpx

from cgbot.config.settings import ConfigError, ProviderConfig, WireApi
from cgbot.provider.chat import ChatCompletionsProvider
from cgbot.provider.errors import BadRequest, ProviderError, RateLimited, UpstreamUnavailable
from cgbot.provider.fallback import FallbackProvider
from cgbot.provider.gemini import GeminiProvider
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
) -> ResponsesProvider | ChatCompletionsProvider | GeminiProvider | FallbackProvider:
    """Adapter for ``cfg``; a :class:`FallbackProvider` (primary first) when fallbacks exist."""
    primary = _build_member(
        wire_api=cfg.wire_api,
        base_url=cfg.base_url,
        api_key=cfg.api_key.reveal(),
        model=cfg.model,
        context_window=cfg.context_window,
        cfg=cfg,
        http=http,
        key="provider.wire_api",
    )
    if not cfg.fallbacks:
        return primary
    members = [primary]
    for index, fb in enumerate(cfg.fallbacks):
        members.append(
            _build_member(
                wire_api=fb.wire_api,
                base_url=fb.base_url,
                api_key=fb.api_key.reveal(),
                model=fb.model,
                context_window=fb.context_window,
                cfg=cfg,
                http=http,
                key=f"provider.fallbacks[{index}].wire_api",
            )
        )
    return FallbackProvider(members)


def _build_member(
    *,
    wire_api: WireApi,
    base_url: str,
    api_key: str,
    model: str,
    context_window: int,
    cfg: ProviderConfig,
    http: httpx.AsyncClient | None,
    key: str,
) -> ResponsesProvider | ChatCompletionsProvider | GeminiProvider:
    """One adapter by wire format; retry settings come from ``cfg`` for every member."""
    if wire_api == "responses":
        adapter: type[ResponsesProvider | ChatCompletionsProvider | GeminiProvider] = (
            ResponsesProvider
        )
    elif wire_api == "chat":
        adapter = ChatCompletionsProvider
    elif wire_api == "gemini":
        adapter = GeminiProvider
    else:
        raise ConfigError(key, f"未知值 {wire_api!r}")
    return adapter(
        base_url=base_url,
        api_key=api_key,
        model=model,
        context_window=context_window,
        http=http,
        retry_attempts=cfg.retry_attempts,
        retry_backoff=cfg.retry_backoff,
    )


__all__ = [
    "BadRequest",
    "ChatCompletionsProvider",
    "Completed",
    "FallbackProvider",
    "FunctionCall",
    "GeminiProvider",
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
