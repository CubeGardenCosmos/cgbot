"""有序降级:主模型失败(适配器自身的重试用尽后)依次尝试后备模型。

每一轮对话都从主模型重新开始,不记忆"上次是谁答的"。流式只在失败成员尚未产出任何事件时降级;
一旦已有事件交给调用方,再换模型会让输出前后拼接,所以原样抛出。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence

from cgbot.provider.errors import ProviderError
from cgbot.provider.types import ModelProvider, ModelRequest, ModelResponse, StreamEvent

logger = logging.getLogger(__name__)


class FallbackProvider:
    """``providers[0]`` is the primary; ``name``/``context_window`` are always the primary's."""

    def __init__(self, providers: Sequence[ModelProvider]) -> None:
        if not providers:
            raise ValueError("FallbackProvider needs at least one provider")
        self._providers = tuple(providers)

    @property
    def name(self) -> str:
        return self._providers[0].name

    @property
    def context_window(self) -> int:
        return self._providers[0].context_window

    async def respond(self, request: ModelRequest) -> ModelResponse:
        last: ProviderError | None = None
        for index, provider in enumerate(self._providers):
            try:
                response = await provider.respond(request)
            except ProviderError as exc:
                last = exc
                _log_failure(provider, exc, has_next=index + 1 < len(self._providers))
                continue
            _log_served(index, provider)
            return response
        assert last is not None
        raise last

    async def respond_stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        last: ProviderError | None = None
        for index, provider in enumerate(self._providers):
            stream = provider.respond_stream(request)
            started = False
            try:
                async for event in stream:
                    if not started:
                        started = True
                        _log_served(index, provider)
                    yield event
                return
            except ProviderError as exc:
                if started:
                    raise
                last = exc
                _log_failure(provider, exc, has_next=index + 1 < len(self._providers))
            finally:
                aclose = getattr(stream, "aclose", None)
                if aclose is not None:
                    await aclose()
        assert last is not None
        raise last

    async def aclose(self) -> None:
        """Close every member that owns resources; all are attempted, the first error is raised."""
        first: Exception | None = None
        for provider in self._providers:
            aclose = getattr(provider, "aclose", None)
            if aclose is None:
                continue
            try:
                await aclose()
            except Exception as exc:
                first = first or exc
        if first is not None:
            raise first


def _log_failure(provider: ModelProvider, exc: ProviderError, *, has_next: bool) -> None:
    logger.warning(
        "model %s failed (%s: %s)%s",
        provider.name,
        type(exc).__name__,
        exc,
        ", falling back" if has_next else "",
    )


def _log_served(index: int, provider: ModelProvider) -> None:
    if index > 0:
        logger.info("fallback model %s answered", provider.name)
