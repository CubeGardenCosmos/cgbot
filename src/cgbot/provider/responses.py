"""OpenAI Responses adapter for the single upstream (R1.5).

Wire format lives only here. Requests are always sent with ``store=false``; the caller sends the
full history each turn. Transport, errors and the result hook come from ``transport.py``.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import aclosing
from typing import Any, ClassVar

from cgbot.provider.errors import ProviderError, UpstreamUnavailable
from cgbot.provider.transport import (
    HttpUpstream,
    as_list,
    count,
    error_status,
    expect_mapping,
    failure_detail,
    parse_arguments,
    sse_events,
)
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

_TERMINAL_EVENTS = frozenset({"response.completed", "response.incomplete"})
_FAILURE_EVENTS = frozenset({"response.failed", "error"})


class ResponsesProvider(HttpUpstream):
    """Single upstream speaking ``POST {base_url}/responses``."""

    _PATH: ClassVar[str] = "/responses"

    async def respond(self, request: ModelRequest) -> ModelResponse:
        status: int | None = None
        try:
            async with self._exchange(request, stream=False) as resp:
                status = resp.status_code
                raw = await resp.aread()
            result = parse_response(self._decode(raw, status))
        except Exception as exc:
            self._report(error_status(exc, status), exc)
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
                async with aclosing(sse_events(resp.aiter_lines())) as events:
                    async for event in events:
                        kind = event.get("type")
                        if kind == "response.output_text.delta":
                            yield TextDelta(text=str(event.get("delta", "")))
                        elif kind == "response.output_item.done":
                            item = parse_output_item(expect_mapping(event.get("item"), "item"))
                            collected.append(item)
                            yield ItemDone(item=item)
                        elif kind in _TERMINAL_EVENTS:
                            final = parse_response(
                                expect_mapping(event.get("response"), "response"), collected
                            )
                            break
                        elif kind in _FAILURE_EVENTS:
                            detail = failure_detail(event, self._api_key)
                            raise UpstreamUnavailable(
                                f"upstream stream failed: {detail}", status=status
                            )
                if final is None:
                    raise UpstreamUnavailable(
                        "upstream stream closed before response.completed", status=status
                    )
        except Exception as exc:
            self._report(error_status(exc, status), exc)
            raise
        self._report(status, None)
        yield Completed(response=final)

    def _body(self, request: ModelRequest, *, stream: bool) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self._model,
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


def parse_response(data: Mapping[str, Any], streamed: Sequence[OutputItem] = ()) -> ModelResponse:
    """Build a ModelResponse from a Responses object.

    ``output`` on the response object is authoritative when present; ``streamed`` (items seen as
    ``output_item.done``) is used only when the terminal object carries no output.
    """
    output = data.get("output")
    if isinstance(output, list) and output:
        items = tuple(parse_output_item(expect_mapping(o, "output item")) for o in output)
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
            for part in as_list(raw.get("content"))
            if isinstance(part, Mapping) and part.get("type") == "output_text"
        )
        return TextOutput(text=text, raw=dict(raw))
    if kind == "function_call":
        arguments = raw.get("arguments", "")
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments, ensure_ascii=False)
        parsed, error = parse_arguments(arguments)
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
            for part in as_list(raw.get("summary"))
            if isinstance(part, Mapping)
        )
        return Reasoning(summary=summary, raw=dict(raw))
    return OtherOutput(type=str(kind), raw=dict(raw))


def _usage(raw: object) -> Usage:
    if not isinstance(raw, Mapping):
        return Usage()
    inp = raw.get("input_tokens_details")
    out = raw.get("output_tokens_details")
    return Usage(
        input_tokens=count(raw.get("input_tokens")),
        output_tokens=count(raw.get("output_tokens")),
        cached=count(inp.get("cached_tokens") if isinstance(inp, Mapping) else None),
        reasoning=count(out.get("reasoning_tokens") if isinstance(out, Mapping) else None),
    )
