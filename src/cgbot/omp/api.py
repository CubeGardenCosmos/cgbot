"""Session-level helpers over an OmpClient: state, busy/queued checks, history paging, redaction.

Pure functions with no bot or framework dependency. Endpoints follow the old plugin:
``/api/sessions/{id}/state``, ``/api/sessions/{id}/context``, ``/api/agent/{id}`` (commands),
``/api/sessions/{id}/subagents``, ``/api/provider-usage``.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import quote

from cgbot.omp.client import OmpClient, OmpError, redact

TRUNCATED_SUFFIX = "…[已截断]"
MSG_BAD_HISTORY_PAGE = "ompweb 响应无效。"


def session_state_path(session_id: str) -> str:
    return f"/api/sessions/{quote(session_id, safe='')}/state"


def command_path(session_id: str) -> str:
    return f"/api/agent/{quote(session_id, safe='')}"


def subagents_path(session_id: str) -> str:
    return f"/api/sessions/{quote(session_id, safe='')}/subagents"


def busy(state: dict[str, Any]) -> bool:
    return any(
        state.get(key)
        for key in ("isStreaming", "isPromptRunning", "isBashRunning", "isCompacting")
    )


def queued(state: dict[str, Any]) -> bool:
    if state.get("queuedMessageCount"):
        return True
    queue = state.get("queuedMessages")
    if isinstance(queue, dict):
        return bool(queue.get("steering") or queue.get("followUp"))
    return False


async def fetch_state(client: OmpClient, session_id: str) -> dict[str, Any]:
    """Return ``{"running": bool, "state": dict}`` for one session."""
    data = await client.get(session_state_path(session_id))
    state = data.get("state")
    return {"running": bool(data.get("running")), "state": state if isinstance(state, dict) else {}}


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + TRUNCATED_SUFFIX


async def history_page(
    client: OmpClient,
    session_id: str,
    cursor: dict[str, Any] | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    path = f"/api/sessions/{quote(session_id, safe='')}/context?sync=1&limit={limit}"
    if cursor:
        path += "&cursor=" + quote(json.dumps(cursor, separators=(",", ":")), safe="")
    return await client.get(path)


async def history_tail(
    client: OmpClient, session_id: str, limit: int = 200
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Walk sync pages (forward-only from the oldest entry) to the tail.

    Returns ``(last_page, latest_assistant_message)``.
    """
    cursor: dict[str, Any] | None = None
    latest: dict[str, Any] = {}
    while True:
        page = await history_page(client, session_id, cursor, limit)
        messages = _messages(page)
        latest = next((m for m in reversed(messages) if m.get("role") == "assistant"), latest)
        next_cursor = page.get("cursor")
        if not page.get("hasMore") or not isinstance(next_cursor, dict):
            return page, latest
        cursor = next_cursor


def _messages(page: dict[str, Any]) -> list[dict[str, Any]]:
    context = page.get("context")
    if not isinstance(context, dict):
        return []
    messages = context.get("messages")
    if not isinstance(messages, list):
        return []
    return [m for m in messages if isinstance(m, dict)]


def history_messages(page: dict[str, Any]) -> list[dict[str, Any]]:
    return _messages(page)


def read_entry(
    message: dict[str, Any], include_thinking: bool, limit: int
) -> dict[str, Any] | None:
    """Turn one ompweb context message into a bounded, model-facing entry."""
    role = message.get("role")
    if role == "custom" and message.get("display") is False:
        return None
    content = message.get("content") or []
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    texts: list[str] = []
    calls: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            texts.append(str(block.get("text", "")))
        elif kind == "thinking" and include_thinking:
            texts.append(str(block.get("thinking", "")))
        elif kind == "toolCall":
            calls.append(
                {
                    "tool": block.get("toolName"),
                    "input": clip(str(block.get("input", "")), limit),
                }
            )
    text = clip("\n".join(texts), limit)
    if role == "toolResult":
        return {
            "role": "tool_result",
            "tool": message.get("toolName"),
            "is_error": bool(message.get("isError")),
            "text": text,
        }
    entry: dict[str, Any] = {"role": role, "text": text}
    if calls:
        entry["tool_calls"] = calls
    return entry


def redact_value(value: Any, secrets: list[str]) -> Any:
    """Recursively redact every string in a JSON-like value."""
    if isinstance(value, str):
        return redact(value, secrets)
    if isinstance(value, dict):
        return {str(k): redact_value(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(v, secrets) for v in value]
    return value


def report_signature(kind: str, summary: str, detail: Any) -> str:
    payload = json.dumps([kind, summary, detail], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def fuzzy_match(needle: str, haystack: str) -> bool:
    """Case-insensitive subsequence match; an empty needle matches everything."""
    if not needle:
        return True
    remaining = iter(haystack.casefold())
    return all(char in remaining for char in needle.casefold())


def required(data: dict[str, Any], key: str) -> Any:
    """Read a required field from a server reply, failing with a safe message."""
    if key not in data or data[key] is None:
        raise OmpError(MSG_BAD_HISTORY_PAGE)
    return data[key]


def mask_account(label: Any) -> str | None:
    if not isinstance(label, str):
        return None
    local, sep, domain = label.rpartition("@")
    if not sep or not local or not domain:
        return None
    return local[0] + "***@" + domain


def mask_text(text: Any) -> str | None:
    if not isinstance(text, str):
        return None
    import re

    return re.sub(r"([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+)", r"\1***@\2", text)
