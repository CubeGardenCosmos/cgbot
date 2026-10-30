"""Chat Completions adapter (R1.5, ``wire_api = "chat"``): ``POST {base_url}/chat/completions``.

Requests are built from the same Responses-shaped history the runner stores (``items.py``), and
results are mapped back to the provider-neutral types. Each result item's ``raw`` is a Responses
input item, so history replays through either adapter. Mapping:

- ``instructions`` -> leading ``system`` message (skipped when empty).
- ``message`` items with role user/assistant/system/developer -> chat messages; text parts only
  (images and other non-text parts are dropped). Empty assistant messages are dropped.
- ``function_call`` -> ``tool_calls`` of an assistant message; ``function_call_output`` -> a
  ``tool`` message with the same id. A call is sent only when its output is present, and outputs
  are emitted right after their call group, so every ``tool_calls`` id is answered. Unpaired
  items are dropped.
- ``reasoning`` and other item types have no chat equivalent and are skipped.
- Results: ``finish_reason`` stop/tool_calls -> completed; length -> incomplete
  (``max_output_tokens``); content_filter -> incomplete (``content_filter``); other -> incomplete.
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
    OutputItem,
    StreamEvent,
    TextDelta,
    TextOutput,
    Usage,
)

_CHAT_ROLES: Mapping[str, str] = {
    "user": "user",
    "assistant": "assistant",
    "system": "system",
    "developer": "system",
}
_TEXT_PART_TYPES = frozenset({"input_text", "output_text", "text"})
_OK_FINISH = frozenset({"stop", "tool_calls", "function_call"})
_INCOMPLETE_REASONS: Mapping[str, str] = {
    "length": "max_output_tokens",
    "content_filter": "content_filter",
}


class ChatCompletionsProvider(HttpUpstream):
    """Upstream speaking ``POST {base_url}/chat/completions``."""

    _PATH: ClassVar[str] = "/chat/completions"

    async def respond(self, request: ModelRequest) -> ModelResponse:
        status: int | None = None
        try:
            async with self._exchange(request, stream=False) as resp:
                status = resp.status_code
                raw = await resp.aread()
            result = parse_completion(self._decode(raw, status))
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
                state = _StreamState()
                async with aclosing(sse_events(resp.aiter_lines())) as events:
                    async for event in events:
                        if "error" in event:
                            detail = failure_detail(event, self._api_key)
                            raise UpstreamUnavailable(
                                f"upstream stream failed: {detail}", status=status
                            )
                        delta = state.absorb(event)
                        if delta:
                            yield TextDelta(text=delta)
                final = state.response(status)
        except Exception as exc:
            self._report(error_status(exc, status), exc)
            raise
        self._report(status, None)
        for item in final.items:
            yield ItemDone(item=item)
        yield Completed(response=final)

    def _body(self, request: ModelRequest, *, stream: bool) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self._model,
            "messages": chat_messages(request),
            "stream": stream,
        }
        if stream:
            body["stream_options"] = {"include_usage": True}
        if request.tools:
            body["tools"] = [chat_tool(tool) for tool in request.tools]
        if request.reasoning_effort is not None:
            body["reasoning_effort"] = request.reasoning_effort
        if request.max_output_tokens is not None:
            body["max_tokens"] = request.max_output_tokens
        return body


def chat_tool(tool: Mapping[str, Any]) -> dict[str, Any]:
    """Responses function tool -> chat function tool. Other tool types cannot be sent."""
    if tool.get("type") != "function":
        raise ValueError(f"chat wire_api supports function tools only, got {tool.get('type')!r}")
    fields = {
        key: tool[key] for key in ("name", "description", "parameters", "strict") if key in tool
    }
    return {"type": "function", "function": fields}


def chat_messages(request: ModelRequest) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    if request.instructions:
        messages.append({"role": "system", "content": request.instructions})
    messages.extend(_convert_input(request.input))
    return messages


def parse_completion(data: Mapping[str, Any]) -> ModelResponse:
    choices = as_list(data.get("choices"))
    if not choices:
        raise ProviderError("upstream response has no choices")
    choice = expect_mapping(choices[0], "choice")
    message = expect_mapping(choice.get("message"), "message")
    reason = choice.get("finish_reason")
    if not isinstance(reason, str):
        raise ProviderError("upstream choice has no finish_reason")
    items: list[OutputItem] = []
    content = message.get("content")
    if isinstance(content, str) and content:
        items.append(_text_item(content))
    for raw_call in as_list(message.get("tool_calls")):
        call = expect_mapping(raw_call, "tool call")
        fn = expect_mapping(call.get("function"), "tool call function")
        items.append(_call_item(call.get("id"), fn.get("name"), fn.get("arguments")))
    status, incomplete = _status(reason)
    return ModelResponse(
        items=tuple(items),
        usage=_usage(data.get("usage")),
        response_id=str(data.get("id", "")),
        status=status,
        incomplete_reason=incomplete,
    )


class _StreamState:
    """Folds chat stream chunks into text and tool-call drafts keyed by ``index``."""

    def __init__(self) -> None:
        self._text: list[str] = []
        self._calls: dict[int, dict[str, str]] = {}
        self._finish: str | None = None
        self._usage: object = None
        self._response_id = ""

    def absorb(self, event: Mapping[str, Any]) -> str:
        """Fold one chunk; returns the text delta it carries (may be empty)."""
        self._response_id = self._response_id or str(event.get("id", ""))
        if event.get("usage") is not None:
            self._usage = event["usage"]
        choices = as_list(event.get("choices"))
        if not choices:
            return ""
        choice = expect_mapping(choices[0], "choice")
        delta = choice.get("delta")
        if not isinstance(delta, Mapping):
            delta = {}
        for raw_call in as_list(delta.get("tool_calls")):
            self._absorb_call(raw_call)
        reason = choice.get("finish_reason")
        if isinstance(reason, str):
            self._finish = reason
        content = delta.get("content")
        if isinstance(content, str) and content:
            self._text.append(content)
            return content
        return ""

    def _absorb_call(self, raw: object) -> None:
        call = expect_mapping(raw, "tool call delta")
        index = call.get("index")
        slot = self._calls.setdefault(
            index if isinstance(index, int) and not isinstance(index, bool) else 0,
            {"id": "", "name": "", "arguments": ""},
        )
        call_id = call.get("id")
        if isinstance(call_id, str) and call_id:
            slot["id"] = call_id
        fn = call.get("function")
        if isinstance(fn, Mapping):
            name = fn.get("name")
            if isinstance(name, str) and name and not slot["name"]:
                slot["name"] = name
            arguments = fn.get("arguments")
            if isinstance(arguments, str):
                slot["arguments"] += arguments

    def response(self, status: int | None) -> ModelResponse:
        if self._finish is None:
            raise UpstreamUnavailable("upstream stream closed before finish_reason", status=status)
        items: list[OutputItem] = []
        text = "".join(self._text)
        if text:
            items.append(_text_item(text))
        for index in sorted(self._calls):
            slot = self._calls[index]
            items.append(_call_item(slot["id"], slot["name"], slot["arguments"]))
        state, incomplete = _status(self._finish)
        return ModelResponse(
            items=tuple(items),
            usage=_usage(self._usage),
            response_id=self._response_id,
            status=state,
            incomplete_reason=incomplete,
        )


def _status(reason: str) -> tuple[str, str | None]:
    if reason in _OK_FINISH:
        return "completed", None
    return "incomplete", _INCOMPLETE_REASONS.get(reason, reason)


def _text_item(text: str) -> TextOutput:
    raw = {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }
    return TextOutput(text=text, raw=raw)


def _call_item(call_id: object, name: object, arguments: object) -> FunctionCall:
    text = _arguments_text(arguments)
    parsed, error = parse_arguments(text)
    raw = {
        "type": "function_call",
        "call_id": str(call_id or ""),
        "name": str(name or ""),
        "arguments": text,
    }
    return FunctionCall(
        call_id=str(call_id or ""),
        name=str(name or ""),
        arguments=text,
        parsed_arguments=parsed,
        parse_error=error,
        raw=raw,
    )


def _arguments_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False)


def _output_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False)


def _text_of(content: object) -> str:
    if isinstance(content, str):
        return content
    return "".join(
        str(part.get("text", ""))
        for part in as_list(content)
        if isinstance(part, Mapping) and part.get("type") in _TEXT_PART_TYPES
    )


def _message(item: Mapping[str, Any]) -> dict[str, Any] | None:
    role = _CHAT_ROLES.get(str(item.get("role")))
    if role is None:
        return None
    text = _text_of(item.get("content"))
    if role == "assistant" and not text:
        return None
    return {"role": role, "content": text}


def _convert_input(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    outputs: dict[str, str] = {}
    for item in items:
        if item.get("type") == "function_call_output":
            outputs.setdefault(str(item.get("call_id", "")), _output_text(item.get("output")))
    messages: list[dict[str, Any]] = []
    group: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        kind = item.get("type")
        if kind == "function_call":
            call_id = str(item.get("call_id", ""))
            if call_id in outputs and call_id not in seen:
                seen.add(call_id)
                group.append(item)
            continue
        if kind == "function_call_output":
            continue
        if group:
            _emit_group(messages, group, outputs)
            group = []
        if kind in (None, "message"):
            message = _message(item)
            if message is not None:
                messages.append(message)
    if group:
        _emit_group(messages, group, outputs)
    return messages


def _emit_group(
    messages: list[dict[str, Any]],
    group: Sequence[Mapping[str, Any]],
    outputs: Mapping[str, str],
) -> None:
    calls = [
        {
            "id": str(call.get("call_id", "")),
            "type": "function",
            "function": {
                "name": str(call.get("name", "")),
                "arguments": _arguments_text(call.get("arguments")) or "{}",
            },
        }
        for call in group
    ]
    previous = messages[-1] if messages else None
    if previous is not None and previous["role"] == "assistant" and "tool_calls" not in previous:
        previous["tool_calls"] = calls
    else:
        messages.append({"role": "assistant", "content": None, "tool_calls": calls})
    for call in calls:
        messages.append(
            {"role": "tool", "tool_call_id": call["id"], "content": outputs[call["id"]]}
        )


def _usage(raw: object) -> Usage:
    if not isinstance(raw, Mapping):
        return Usage()
    prompt = raw.get("prompt_tokens_details")
    completion = raw.get("completion_tokens_details")
    cached = prompt.get("cached_tokens") if isinstance(prompt, Mapping) else None
    if cached is None:
        cached = raw.get("prompt_cache_hit_tokens")
    return Usage(
        input_tokens=count(raw.get("prompt_tokens")),
        output_tokens=count(raw.get("completion_tokens")),
        cached=count(cached),
        reasoning=count(
            completion.get("reasoning_tokens") if isinstance(completion, Mapping) else None
        ),
    )
