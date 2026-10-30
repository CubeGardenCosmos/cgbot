"""OpenAI Responses adapter for the single upstream (R1.5).

Wire format lives only here. Requests are always sent with ``store=false``; the caller sends the
full history each turn. ``on_result(status, error)`` is called once per request outcome:
``status`` is the HTTP status when one was received (else None), ``error`` is None on success.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping, Sequence
from contextlib import aclosing, asynccontextmanager
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from cgbot.provider.errors import BadRequest, ProviderError, RateLimited, UpstreamUnavailable
from cgbot.provider.types import (
    Completed,
    FunctionCall,
    ItemDone,
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

ResultHook = Callable[[int | None, Exception | None], None]

_EXCERPT_LIMIT = 500
_TERMINAL_EVENTS = frozenset({"response.completed", "response.incomplete"})
_FAILURE_EVENTS = frozenset({"response.failed", "error"})


class ResponsesProvider:
    """Single upstream speaking ``POST {base_url}/responses``.

    ``context_window`` comes from configuration; ``name`` is the model id. Passing ``http``
    keeps the caller in charge of the client; otherwise an owned client is closed by ``aclose``.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        context_window: int,
        http: httpx.AsyncClient | None = None,
        timeout: float = 120.0,
        on_result: ResultHook | None = None,
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

    def __repr__(self) -> str:
        return (
            f"ResponsesProvider(base_url={self._base_url!r}, model={self._model!r}, "
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

    async def respond(self, request: ModelRequest) -> ModelResponse:
        status: int | None = None
        try:
            async with self._exchange(request, stream=False) as resp:
                status = resp.status_code
                raw = await resp.aread()
            result = parse_response(self._decode(raw, status))
        except Exception as exc:
            self._report(_error_status(exc, status), exc)
            raise
        self._report(status, None)
        return result

    async def respond_stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        status: int | None = None
        final: ModelResponse | None = None
        try:
            async with self._exchange(request, stream=True) as resp:
                status = resp.status_code
                collected: list[OutputItem] = []
                async with aclosing(_sse_events(resp.aiter_lines())) as events:
                    async for event in events:
                        kind = event.get("type")
                        if kind == "response.output_text.delta":
                            yield TextDelta(text=str(event.get("delta", "")))
                        elif kind == "response.output_item.done":
                            item = parse_output_item(_expect_mapping(event.get("item"), "item"))
                            collected.append(item)
                            yield ItemDone(item=item)
                        elif kind in _TERMINAL_EVENTS:
                            final = parse_response(
                                _expect_mapping(event.get("response"), "response"), collected
                            )
                            break
                        elif kind in _FAILURE_EVENTS:
                            detail = _failure_detail(event, self._api_key)
                            raise UpstreamUnavailable(
                                f"upstream stream failed: {detail}", status=status
                            )
                if final is None:
                    raise UpstreamUnavailable(
                        "upstream stream closed before response.completed", status=status
                    )
        except Exception as exc:
            self._report(_error_status(exc, status), exc)
            raise
        self._report(status, None)
        yield Completed(response=final)

    @asynccontextmanager
    async def _exchange(
        self, request: ModelRequest, *, stream: bool
    ) -> AsyncIterator[httpx.Response]:
        """Open the request; raise a mapped ProviderError for non-2xx or transport failures."""
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "text/event-stream" if stream else "application/json",
        }
        body = _request_body(request, self._model, stream=stream)
        try:
            async with self._http.stream(
                "POST",
                f"{self._base_url}/responses",
                json=body,
                headers=headers,
                timeout=self._timeout,
            ) as resp:
                if not resp.is_success:
                    await resp.aread()
                    raise _http_error(resp, self._api_key)
                yield resp
        except httpx.TimeoutException as exc:
            raise UpstreamUnavailable("upstream request timed out") from exc
        except httpx.TransportError as exc:
            raise UpstreamUnavailable(f"upstream unreachable: {type(exc).__name__}") from exc

    def _decode(self, raw: bytes, status: int) -> Mapping[str, Any]:
        try:
            data: object = json.loads(raw)
        except ValueError as exc:
            raise ProviderError(
                "upstream returned a non-JSON body",
                status=status,
                body=_excerpt(raw.decode("utf-8", "replace"), self._api_key),
            ) from exc
        if not isinstance(data, dict):
            raise ProviderError("upstream returned a non-object JSON body", status=status)
        return data

    def _report(self, status: int | None, error: Exception | None) -> None:
        if self.on_result is not None:
            self.on_result(status, error)


def parse_response(data: Mapping[str, Any], streamed: Sequence[OutputItem] = ()) -> ModelResponse:
    """Build a ModelResponse from a Responses object.

    ``output`` on the response object is authoritative when present; ``streamed`` (items seen as
    ``output_item.done``) is used only when the terminal object carries no output.
    """
    output = data.get("output")
    if isinstance(output, list) and output:
        items = tuple(parse_output_item(_expect_mapping(o, "output item")) for o in output)
    else:
        items = tuple(streamed)
    status = data.get("status")
    if not isinstance(status, str):
        raise ProviderError("upstream response has no status")
    details = data.get("incomplete_details")
    reason = details.get("reason") if isinstance(details, Mapping) else None
    return ModelResponse(
        items=items,
        usage=_usage(data.get("usage")),
        response_id=str(data.get("id", "")),
        status=status,
        incomplete_reason=str(reason) if reason is not None else None,
    )


def parse_output_item(raw: Mapping[str, Any]) -> OutputItem:
    kind = raw.get("type")
    if kind == "message":
        text = "".join(
            str(part.get("text", ""))
            for part in _as_list(raw.get("content"))
            if isinstance(part, Mapping) and part.get("type") == "output_text"
        )
        return TextOutput(text=text, raw=dict(raw))
    if kind == "function_call":
        arguments = raw.get("arguments", "")
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments, ensure_ascii=False)
        parsed, error = _parse_arguments(arguments)
        return FunctionCall(
            call_id=str(raw.get("call_id", "")),
            name=str(raw.get("name", "")),
            arguments=arguments,
            parsed_arguments=parsed,
            parse_error=error,
            raw=dict(raw),
        )
    if kind == "reasoning":
        summary = "\n".join(
            str(part.get("text", ""))
            for part in _as_list(raw.get("summary"))
            if isinstance(part, Mapping)
        )
        return Reasoning(summary=summary, raw=dict(raw))
    return OtherOutput(type=str(kind), raw=dict(raw))


async def _sse_events(lines: AsyncIterator[str]) -> AsyncGenerator[Mapping[str, Any]]:
    """Yield decoded ``data:`` payloads. The event's ``type`` lives in the payload itself."""
    data_lines: list[str] = []
    async for line in lines:
        if line == "":
            if data_lines:
                payload = "\n".join(data_lines)
                data_lines = []
                if payload == "[DONE]":
                    return
                yield _decode_event(payload)
        elif line.startswith("data:"):
            value = line[5:]
            data_lines.append(value[1:] if value.startswith(" ") else value)
    if data_lines:
        payload = "\n".join(data_lines)
        if payload != "[DONE]":
            yield _decode_event(payload)


def _decode_event(payload: str) -> Mapping[str, Any]:
    try:
        event: object = json.loads(payload)
    except ValueError as exc:
        raise ProviderError("malformed SSE event from upstream") from exc
    if not isinstance(event, dict):
        raise ProviderError("SSE event from upstream is not a JSON object")
    return event


def _request_body(request: ModelRequest, model: str, *, stream: bool) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "instructions": request.instructions,
        "input": request.input,
        "store": False,
        "stream": stream,
    }
    if request.tools:
        body["tools"] = request.tools
    if request.reasoning_effort is not None:
        body["reasoning"] = {"effort": request.reasoning_effort}
    if request.max_output_tokens is not None:
        body["max_output_tokens"] = request.max_output_tokens
    return body


def _http_error(resp: httpx.Response, api_key: str) -> ProviderError:
    status = resp.status_code
    body = _excerpt(resp.text, api_key)
    message = f"upstream returned HTTP {status}: {body}"
    if status == 429:
        return RateLimited(
            message,
            retry_after=_retry_after(resp.headers.get("retry-after")),
            status=status,
            body=body,
        )
    if status >= 500:
        return UpstreamUnavailable(message, status=status, body=body)
    if 400 <= status < 500:
        return BadRequest(message, status=status, body=body)
    return ProviderError(message, status=status, body=body)


def _failure_detail(event: Mapping[str, Any], api_key: str) -> str:
    response = event.get("response")
    error: object = event.get("error")
    if isinstance(response, Mapping):
        error = response.get("error", error)
    if isinstance(error, Mapping):
        text = f"{error.get('code', '')}: {error.get('message', '')}"
    else:
        text = str(event.get("message", event.get("type", "unknown")))
    return _excerpt(text, api_key)


def _usage(raw: object) -> Usage:
    if not isinstance(raw, Mapping):
        return Usage()
    inp = raw.get("input_tokens_details")
    out = raw.get("output_tokens_details")
    return Usage(
        input_tokens=_count(raw.get("input_tokens")),
        output_tokens=_count(raw.get("output_tokens")),
        cached=_count(inp.get("cached_tokens") if isinstance(inp, Mapping) else None),
        reasoning=_count(out.get("reasoning_tokens") if isinstance(out, Mapping) else None),
    )


def _parse_arguments(text: str) -> tuple[Mapping[str, Any] | None, str | None]:
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


def _retry_after(value: str | None) -> float | None:
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


def _excerpt(text: str, api_key: str) -> str:
    if api_key:
        text = text.replace(api_key, "***")
    text = " ".join(text.split())
    if len(text) > _EXCERPT_LIMIT:
        return text[:_EXCERPT_LIMIT] + "...(truncated)"
    return text


def _error_status(exc: Exception, fallback: int | None) -> int | None:
    if isinstance(exc, ProviderError) and exc.status is not None:
        return exc.status
    return fallback


def _expect_mapping(value: object, what: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProviderError(f"upstream {what} is missing or malformed")
    return value


def _as_list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0
