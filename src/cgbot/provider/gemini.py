"""Gemini 原生适配器(``wire_api = "gemini"``)。

非流式 ``POST {base_url}/models/{model}:generateContent``,流式
``POST {base_url}/models/{model}:streamGenerateContent?alt=sse``;密钥只放在 ``x-goog-api-key``
请求头,从不进 URL。请求由 runner 存储的 Responses 形态历史(``items.py``)构造,结果映射回
中立类型,每个结果项的 ``raw`` 都是 Responses 输入项,历史因此与模型无关。映射:

- ``instructions`` -> ``systemInstruction``(为空则省略)。
- ``message``:user/system/developer -> ``user`` 内容;assistant -> ``model`` 内容。文本片段成
  ``text`` part;user 的 ``input_image`` data URL 成 ``inlineData{mimeType,data}``(非 data URL
  与其他角色的图片丢弃)。相邻同角色内容合并。
- ``function_call`` -> ``model`` 内容里的 ``functionCall{name,args}``(args 为解析后的 JSON
  对象);``function_call_output`` -> 紧随其后的 ``user`` 内容里的
  ``functionResponse{name,response:{output}}``,name 由 call_id 反查。与 chat 适配器一致:
  无输出的调用与无调用的输出都丢弃。每个 functionCall 都带
  ``thoughtSignature: "skip_thought_signature_validator"``(Google 文档中的跨模型历史绕过值)。
- ``reasoning`` 等其他项无对应物,跳过。
- 工具:function 工具 -> ``tools[0].functionDeclarations``,schema 放 ``parametersJsonSchema``。
- ``max_output_tokens`` -> ``generationConfig.maxOutputTokens``;``reasoning_effort`` ->
  ``generationConfig.thinkingConfig.thinkingLevel``(未知值省略)。
- 结果:非 thought 文本 -> TextOutput;functionCall -> FunctionCall;thought part 与
  thoughtSignature 一律丢弃(不进历史)。finishReason STOP -> completed;MAX_TOKENS ->
  incomplete(``max_output_tokens``);SAFETY 等内容拦截与 promptFeedback.blockReason ->
  BadRequest;其余 -> incomplete。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import aclosing
from typing import Any

from cgbot.provider.errors import BadRequest, ProviderError, UpstreamUnavailable
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

SKIP_SIGNATURE = "skip_thought_signature_validator"

_TEXT_PART_TYPES = frozenset({"input_text", "output_text", "text"})
_USER_ROLES = frozenset({"user", "system", "developer"})
_THINKING_LEVELS: Mapping[str, str] = {
    "minimal": "minimal",
    "none": "minimal",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "max": "high",
    "xhigh": "high",
}
_BLOCKED_FINISH = frozenset(
    {
        "SAFETY",
        "PROHIBITED_CONTENT",
        "BLOCKLIST",
        "SPII",
        "IMAGE_SAFETY",
        "RECITATION",
        "LANGUAGE",
    }
)
_INCOMPLETE_REASONS: Mapping[str, str] = {"MAX_TOKENS": "max_output_tokens"}


class GeminiProvider(HttpUpstream):
    """Upstream speaking the native Gemini ``generateContent`` API."""

    async def respond(self, request: ModelRequest) -> ModelResponse:
        status: int | None = None
        try:
            async with self._exchange(request, stream=False) as resp:
                status = resp.status_code
                raw = await resp.aread()
            result = parse_generate(self._decode(raw, status), status)
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
                        delta = state.absorb(event, status)
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

    def _url(self, stream: bool) -> str:
        method = "streamGenerateContent?alt=sse" if stream else "generateContent"
        return f"{self._base_url}/models/{self._model}:{method}"

    def _auth_headers(self) -> dict[str, str]:
        return {"x-goog-api-key": self._api_key}

    def _body(self, request: ModelRequest, *, stream: bool) -> dict[str, Any]:
        body: dict[str, Any] = {"contents": gemini_contents(request.input)}
        if request.instructions:
            body["systemInstruction"] = {"parts": [{"text": request.instructions}]}
        declarations = [gemini_declaration(tool) for tool in request.tools]
        if declarations:
            body["tools"] = [{"functionDeclarations": declarations}]
        config: dict[str, Any] = {}
        if request.max_output_tokens is not None:
            config["maxOutputTokens"] = request.max_output_tokens
        level = _THINKING_LEVELS.get((request.reasoning_effort or "").lower())
        if level is not None:
            config["thinkingConfig"] = {"thinkingLevel": level}
        if config:
            body["generationConfig"] = config
        return body


def gemini_declaration(tool: Mapping[str, Any]) -> dict[str, Any]:
    """Responses function tool -> Gemini function declaration. Other tool types cannot be sent."""
    if tool.get("type") != "function":
        raise ValueError(f"gemini wire_api supports function tools only, got {tool.get('type')!r}")
    fields: dict[str, Any] = {"name": tool["name"]}
    if "description" in tool:
        fields["description"] = tool["description"]
    if tool.get("parameters") is not None:
        fields["parametersJsonSchema"] = tool["parameters"]
    return fields


def gemini_contents(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    outputs: dict[str, str] = {}
    for item in items:
        if item.get("type") == "function_call_output":
            outputs.setdefault(str(item.get("call_id", "")), _output_text(item.get("output")))
    contents: list[dict[str, Any]] = []
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
            _emit_group(contents, group, outputs)
            group = []
        if kind in (None, "message"):
            _emit_message(contents, item)
    if group:
        _emit_group(contents, group, outputs)
    return contents


def _push(contents: list[dict[str, Any]], role: str, parts: list[dict[str, Any]]) -> None:
    if not parts:
        return
    if contents and contents[-1]["role"] == role:
        contents[-1]["parts"].extend(parts)
    else:
        contents.append({"role": role, "parts": parts})


def _emit_message(contents: list[dict[str, Any]], item: Mapping[str, Any]) -> None:
    role = str(item.get("role"))
    if role == "assistant":
        text = _text_of(item.get("content"))
        _push(contents, "model", [{"text": text}] if text else [])
    elif role in _USER_ROLES:
        _push(contents, "user", _user_parts(item.get("content"), images=role == "user"))


def _user_parts(content: object, *, images: bool) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"text": content}] if content else []
    parts: list[dict[str, Any]] = []
    for part in as_list(content):
        if not isinstance(part, Mapping):
            continue
        if part.get("type") in _TEXT_PART_TYPES:
            text = str(part.get("text", ""))
            if text:
                parts.append({"text": text})
        elif part.get("type") == "input_image" and images:
            inline = _inline_data(part)
            if inline is not None:
                parts.append({"inlineData": inline})
    return parts


def _inline_data(part: Mapping[str, Any]) -> dict[str, str] | None:
    value = part.get("image_url")
    if isinstance(value, Mapping):
        value = value.get("url")
    if not isinstance(value, str) or not value.startswith("data:"):
        return None
    header, sep, data = value[5:].partition(",")
    mime, _, encoding = header.partition(";")
    if not sep or not mime or encoding != "base64" or not data:
        return None
    return {"mimeType": mime, "data": data}


def _emit_group(
    contents: list[dict[str, Any]],
    group: Sequence[Mapping[str, Any]],
    outputs: Mapping[str, str],
) -> None:
    calls: list[dict[str, Any]] = []
    responses: list[dict[str, Any]] = []
    for call in group:
        name = str(call.get("name", ""))
        parsed, _ = parse_arguments(_output_text(call.get("arguments")))
        calls.append(
            {
                "functionCall": {"name": name, "args": dict(parsed) if parsed else {}},
                "thoughtSignature": SKIP_SIGNATURE,
            }
        )
        responses.append(
            {
                "functionResponse": {
                    "name": name,
                    "response": {"output": outputs[str(call.get("call_id", ""))]},
                }
            }
        )
    _push(contents, "model", calls)
    _push(contents, "user", responses)


def parse_generate(data: Mapping[str, Any], status: int | None = None) -> ModelResponse:
    state = _StreamState()
    state.absorb(data, status)
    return state.response(status)


class _StreamState:
    """Folds ``GenerateContentResponse`` chunks (a single one when not streaming)."""

    def __init__(self) -> None:
        self._text: list[str] = []
        self._calls: list[FunctionCall] = []
        self._finish: str | None = None
        self._usage: object = None
        self._response_id = ""

    def absorb(self, event: Mapping[str, Any], status: int | None) -> str:
        """Fold one chunk; returns the text delta it carries (may be empty)."""
        self._response_id = self._response_id or str(event.get("responseId", ""))
        if event.get("usageMetadata") is not None:
            self._usage = event["usageMetadata"]
        feedback = event.get("promptFeedback")
        if isinstance(feedback, Mapping) and feedback.get("blockReason"):
            raise BadRequest(
                f"upstream blocked the prompt: {feedback['blockReason']}", status=status
            )
        candidates = as_list(event.get("candidates"))
        if not candidates:
            return ""
        candidate = expect_mapping(candidates[0], "candidate")
        content = candidate.get("content")
        delta = ""
        for raw_part in as_list(content.get("parts")) if isinstance(content, Mapping) else []:
            part = expect_mapping(raw_part, "part")
            if part.get("thought") is True:
                continue
            call = part.get("functionCall")
            if isinstance(call, Mapping):
                self._calls.append(_call_item(call))
                continue
            text = part.get("text")
            if isinstance(text, str) and text:
                self._text.append(text)
                delta += text
        reason = candidate.get("finishReason")
        if isinstance(reason, str) and reason:
            if reason in _BLOCKED_FINISH:
                raise BadRequest(f"upstream blocked the response: {reason}", status=status)
            self._finish = reason
        return delta

    def response(self, status: int | None) -> ModelResponse:
        if self._finish is None:
            if self._usage is None and not self._text and not self._calls:
                raise ProviderError("upstream response has no candidates", status=status)
            raise UpstreamUnavailable("upstream response ended without finishReason", status=status)
        items: list[OutputItem] = []
        text = "".join(self._text)
        if text:
            items.append(_text_item(text))
        items.extend(self._calls)
        if self._finish in ("STOP", "FINISH_REASON_UNSPECIFIED"):
            state, incomplete = "completed", None
        else:
            state = "incomplete"
            incomplete = _INCOMPLETE_REASONS.get(self._finish, self._finish.lower())
        return ModelResponse(
            items=tuple(items),
            usage=_usage(self._usage),
            response_id=self._response_id,
            status=state,
            incomplete_reason=incomplete,
        )


def _text_item(text: str) -> TextOutput:
    raw = {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }
    return TextOutput(text=text, raw=raw)


def _call_item(call: Mapping[str, Any]) -> FunctionCall:
    raw_id = call.get("id")
    call_id = raw_id if isinstance(raw_id, str) and raw_id else f"call_{uuid.uuid4().hex[:24]}"
    name = str(call.get("name") or "")
    args = call.get("args")
    text = json.dumps(args if args is not None else {}, ensure_ascii=False)
    parsed, error = parse_arguments(text)
    raw = {"type": "function_call", "call_id": call_id, "name": name, "arguments": text}
    return FunctionCall(
        call_id=call_id,
        name=name,
        arguments=text,
        parsed_arguments=parsed,
        parse_error=error,
        raw=raw,
    )


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


def _usage(raw: object) -> Usage:
    if not isinstance(raw, Mapping):
        return Usage()
    thoughts = count(raw.get("thoughtsTokenCount"))
    return Usage(
        input_tokens=count(raw.get("promptTokenCount")),
        output_tokens=count(raw.get("candidatesTokenCount")) + thoughts,
        cached=count(raw.get("cachedContentTokenCount")),
        reasoning=thoughts,
    )
