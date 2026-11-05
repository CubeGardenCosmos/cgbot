"""HTTP transport shared by the wire-format adapters (R1.5).

Each adapter (``responses.py``, ``chat.py``) owns its request and response shapes. Everything
that is identical on the wire for both lives here: auth header, timeout, error taxonomy, SSE
framing, key scrubbing and the ``on_result(status, error)`` hook, which is called once per request
outcome (``status`` is the HTTP status when one was received, else None; ``error`` is None on
success).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, ClassVar

import httpx

from cgbot.config.settings import DEFAULT_RETRY_ATTEMPTS, DEFAULT_RETRY_BACKOFF
from cgbot.provider.errors import BadRequest, ProviderError, RateLimited, UpstreamUnavailable
from cgbot.provider.types import ModelRequest

log = logging.getLogger(__name__)

ResultHook = Callable[[int | None, Exception | None], None]

_EXCERPT_LIMIT = 500

RETRY_AFTER_CAP = 30.0


def _transient(exc: ProviderError) -> bool:
    """Failures worth retrying before any response is open: transport, timeout, 5xx, 429."""
    return isinstance(exc, RateLimited | UpstreamUnavailable)


class HttpUpstream:
    """One upstream reached as ``POST {base_url}{_PATH}``. Subclasses supply path and body.

    ``context_window`` comes from configuration; ``name`` is the model id. Passing ``http``
    keeps the caller in charge of the client; otherwise an owned client is closed by ``aclose``.
    """

    _PATH: ClassVar[str]

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        context_window: int,
        http: httpx.AsyncClient | None = None,
        timeout: float = 120.0,
        on_result: ResultHook | None = None,
        retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
        retry_backoff: Sequence[float] = DEFAULT_RETRY_BACKOFF,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if context_window <= 0:
            raise ValueError("context_window must be positive")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._context_window = context_window
        self._timeout = timeout
        self._owns_http = http is None
        self._http = http if http is not None else httpx.AsyncClient()
        self.on_result = on_result
        if retry_attempts < 0:
            raise ValueError("retry_attempts must not be negative")
        self._retry_attempts = retry_attempts
        self._retry_backoff = tuple(retry_backoff)
        self._sleep = sleep

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(base_url={self._base_url!r}, model={self._model!r}, "
            f"context_window={self._context_window}, api_key='***')"
        )

    @property
    def name(self) -> str:
        return self._model

    @property
    def context_window(self) -> int:
        return self._context_window

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    def _body(self, request: ModelRequest, *, stream: bool) -> dict[str, Any]:
        raise NotImplementedError

    @asynccontextmanager
    async def _exchange(
        self, request: ModelRequest, *, stream: bool
    ) -> AsyncIterator[httpx.Response]:
        """Open the request; raise a mapped ProviderError for non-2xx or transport failures.

        Transient failures before a 2xx response is open are retried (see ``_open``). Once the
        caller holds the response nothing is retried: body or stream errors surface as-is.
        """
        stack, resp = await self._open(request, stream)
        try:
            yield resp
        except httpx.TimeoutException as exc:
            raise UpstreamUnavailable("upstream request timed out") from exc
        except httpx.TransportError as exc:
            raise UpstreamUnavailable(f"upstream unreachable: {type(exc).__name__}") from exc
        finally:
            await stack.aclose()

    async def _open(
        self, request: ModelRequest, stream: bool
    ) -> tuple[AsyncExitStack, httpx.Response]:
        for attempt in range(self._retry_attempts + 1):
            stack = AsyncExitStack()
            try:
                return stack, await self._open_once(stack, request, stream)
            except ProviderError as exc:
                await stack.aclose()
                if attempt >= self._retry_attempts or not _transient(exc):
                    raise
                delay = self._retry_delay(attempt, exc)
                log.warning(
                    "provider request failed, retrying: attempt=%d/%d delay=%.1fs error=%s: %s",
                    attempt + 1,
                    self._retry_attempts + 1,
                    delay,
                    type(exc).__name__,
                    exc,
                )
                await self._sleep(delay)
            except BaseException:
                await stack.aclose()
                raise
        raise AssertionError("unreachable")  # pragma: no cover

    async def _open_once(
        self, stack: AsyncExitStack, request: ModelRequest, stream: bool
    ) -> httpx.Response:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "text/event-stream" if stream else "application/json",
        }
        body = self._body(request, stream=stream)
        try:
            resp = await stack.enter_async_context(
                self._http.stream(
                    "POST",
                    f"{self._base_url}{self._PATH}",
                    json=body,
                    headers=headers,
                    timeout=self._timeout,
                )
            )
            if not resp.is_success:
                await resp.aread()
                raise http_error(resp, self._api_key)
        except httpx.TimeoutException as exc:
            raise UpstreamUnavailable("upstream request timed out") from exc
        except httpx.TransportError as exc:
            raise UpstreamUnavailable(f"upstream unreachable: {type(exc).__name__}") from exc
        return resp

    def _retry_delay(self, attempt: int, exc: ProviderError) -> float:
        if isinstance(exc, RateLimited) and exc.retry_after is not None:
            return min(exc.retry_after, RETRY_AFTER_CAP)
        if not self._retry_backoff:
            return 0.0
        return self._retry_backoff[min(attempt, len(self._retry_backoff) - 1)]

    def _decode(self, raw: bytes, status: int) -> Mapping[str, Any]:
        try:
            data: object = json.loads(raw)
        except ValueError as exc:
            raise ProviderError(
                "upstream returned a non-JSON body",
                status=status,
                body=excerpt(raw.decode("utf-8", "replace"), self._api_key),
            ) from exc
        if not isinstance(data, dict):
            raise ProviderError("upstream returned a non-object JSON body", status=status)
        return data

    def _report(self, status: int | None, error: Exception | None) -> None:
        if self.on_result is not None:
            self.on_result(status, error)


def http_error(resp: httpx.Response, api_key: str) -> ProviderError:
    status = resp.status_code
    body = excerpt(resp.text, api_key)
    message = f"upstream returned HTTP {status}: {body}"
    if status == 429:
        return RateLimited(
            message,
            retry_after=retry_after(resp.headers.get("retry-after")),
            status=status,
            body=body,
        )
    if status >= 500:
        return UpstreamUnavailable(message, status=status, body=body)
    if 400 <= status < 500:
        return BadRequest(message, status=status, body=body)
    return ProviderError(message, status=status, body=body)


async def sse_events(lines: AsyncIterator[str]) -> AsyncGenerator[Mapping[str, Any]]:
    """Yield decoded JSON objects from ``data:`` lines; stops at ``[DONE]``.

    Multi-line ``data`` fields are joined with newlines per the SSE spec. The event's type (if
    any) lives in the payload itself, so ``event:`` lines are ignored.
    """
    data_lines: list[str] = []
    async for line in lines:
        if line == "":
            if data_lines:
                payload = "\n".join(data_lines)
                data_lines = []
                if payload == "[DONE]":
                    return
                yield decode_event(payload)
        elif line.startswith("data:"):
            value = line[5:]
            data_lines.append(value[1:] if value.startswith(" ") else value)
    if data_lines:
        payload = "\n".join(data_lines)
        if payload != "[DONE]":
            yield decode_event(payload)


def decode_event(payload: str) -> Mapping[str, Any]:
    try:
        event: object = json.loads(payload)
    except ValueError as exc:
        raise ProviderError("malformed SSE event from upstream") from exc
    if not isinstance(event, dict):
        raise ProviderError("SSE event from upstream is not a JSON object")
    return event


def failure_detail(event: Mapping[str, Any], api_key: str) -> str:
    """Scrubbed ``code: message`` from a stream failure event (Responses or chat shape)."""
    response = event.get("response")
    error: object = event.get("error")
    if isinstance(response, Mapping):
        error = response.get("error", error)
    if isinstance(error, Mapping):
        text = f"{error.get('code', '')}: {error.get('message', '')}"
    else:
        text = str(event.get("message", event.get("type", "unknown")))
    return excerpt(text, api_key)


def parse_arguments(text: str) -> tuple[Mapping[str, Any] | None, str | None]:
    """Parse a tool call's JSON arguments. Empty text is ``{}``; errors carry position only."""
    if not text.strip():
        return {}, None
    try:
        value: object = json.loads(text)
    except ValueError as exc:
        # Position only: the raw arguments may contain user content and stay out of messages.
        return None, f"invalid JSON at char {getattr(exc, 'pos', 0)}"
    if not isinstance(value, dict):
        return None, "arguments are not a JSON object"
    return value, None


def retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def excerpt(text: str, api_key: str) -> str:
    if api_key:
        text = text.replace(api_key, "***")
    text = " ".join(text.split())
    if len(text) > _EXCERPT_LIMIT:
        return text[:_EXCERPT_LIMIT] + "...(truncated)"
    return text


def error_status(exc: Exception, fallback: int | None) -> int | None:
    if isinstance(exc, ProviderError) and exc.status is not None:
        return exc.status
    return fallback


def expect_mapping(value: object, what: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProviderError(f"upstream {what} is missing or malformed")
    return value


def as_list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0
