"""Authenticated sandbox search bridge; upstream credentials remain in the bot."""

from __future__ import annotations

import json
from typing import Any

from aiohttp import web

from cgbot.coding.auth import bearer_token
from cgbot.coding.contracts import SandboxAuth
from cgbot.search.gemini import (
    ERROR_MESSAGES,
    RECENCY_VALUES,
    GeminiSearchClient,
    format_answer,
)
from cgbot.search.gemini import (
    MAX_QUERY_CHARS as GEMINI_MAX_QUERY_CHARS,
)
from cgbot.search.models import (
    E_BAD_ARG,
    E_EMPTY_QUERY,
    E_ENGINE_DISABLED,
    E_UNKNOWN_ENGINE,
    FRESHNESS_VALUES,
)
from cgbot.search.service import MAX_QUERY_CHARS, SearchService
from cgbot.search.tools import format_failure, format_outcome

MAX_BODY_BYTES = 16 * 1024


class _InvalidRequest(Exception):
    def __init__(self, status: int, message: str) -> None:
        self.status = status
        self.message = message


def _error(status: int, code: str, message: str) -> web.Response:
    return web.json_response({"error": code, "message": message}, status=status)


def _authorized(request: web.Request, auth: SandboxAuth) -> bool:
    token = bearer_token(request.headers.get("Authorization"))
    return token is not None and auth.resolve(token) is not None


async def _body(request: web.Request, allowed: set[str]) -> dict[str, Any]:
    if request.content_type != "application/json":
        raise _InvalidRequest(415, "request content type must be application/json")
    if request.content_length is not None and request.content_length > MAX_BODY_BYTES:
        raise _InvalidRequest(413, "request body is too large")
    data = bytearray()
    async for chunk in request.content.iter_chunked(4096):
        data.extend(chunk)
        if len(data) > MAX_BODY_BYTES:
            raise _InvalidRequest(413, "request body is too large")
    try:
        body = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        raise _InvalidRequest(400, "request body must be JSON") from None
    if not isinstance(body, dict):
        raise _InvalidRequest(400, "request body must be a JSON object")
    if set(body) - allowed:
        raise _InvalidRequest(400, "request contains unknown arguments")
    return body


def _query(body: dict[str, Any], limit: int, *, controls: bool = False) -> str:
    value = body.get("query")
    if not isinstance(value, str) or not value.strip():
        raise _InvalidRequest(400, "query must be a non-empty string")
    text = value.strip()
    if len(text) > limit:
        raise _InvalidRequest(400, f"query must be at most {limit} characters")
    if controls and any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise _InvalidRequest(400, "query must not contain control characters")
    return text


def _period(body: dict[str, Any], key: str, values: tuple[str, ...]) -> str | None:
    value = body.get(key)
    if value is not None and (not isinstance(value, str) or value not in values):
        raise _InvalidRequest(400, f"{key} must be one of day/week/month/year")
    return value


def search_routes(
    search: SearchService | None,
    gemini: GeminiSearchClient | None,
    auth: SandboxAuth,
) -> list[web.RouteDef]:
    """Expose search results as model-ready text, never accepting a body-supplied identity."""

    async def web_search(request: web.Request) -> web.Response:
        if not _authorized(request, auth):
            return _error(401, "unauthorized", "Unauthorized")
        if search is None:
            return _error(404, "E_DISABLED", "Web search is disabled")
        try:
            body = await _body(request, {"query", "engine", "count", "freshness"})
            query = _query(body, MAX_QUERY_CHARS)
            engine = body.get("engine")
            if engine is not None and not isinstance(engine, str):
                raise _InvalidRequest(400, "engine must be a string")
            count = body.get("count")
            if count is not None and (
                not isinstance(count, int)
                or isinstance(count, bool)
                or not 1 <= count <= search.config.max_count
            ):
                raise _InvalidRequest(400, f"count must be an integer 1-{search.config.max_count}")
            freshness = _period(body, "freshness", FRESHNESS_VALUES)
        except _InvalidRequest as error:
            return _error(error.status, "invalid_request", error.message)
        outcome = await search.search(query, engine=engine, count=count, freshness=freshness)
        if not outcome.ok:
            status = (
                400
                if outcome.code in {E_BAD_ARG, E_EMPTY_QUERY, E_ENGINE_DISABLED, E_UNKNOWN_ENGINE}
                else 502
            )
            return _error(status, outcome.code, format_failure(outcome))
        return web.Response(text=format_outcome(query, outcome))

    async def gemini_search(request: web.Request) -> web.Response:
        if not _authorized(request, auth):
            return _error(401, "unauthorized", "Unauthorized")
        if gemini is None:
            return _error(404, "E_DISABLED", "Gemini search is disabled")
        try:
            body = await _body(request, {"query", "recency"})
            query = _query(body, GEMINI_MAX_QUERY_CHARS, controls=True)
            recency = _period(body, "recency", RECENCY_VALUES)
        except _InvalidRequest as error:
            return _error(error.status, "invalid_request", error.message)
        result = await gemini.search(query, recency)
        if not result.ok:
            code = result.code or "E_HTTP"
            status = 422 if code == "E_REFUSED" else 502
            if code in {"E_BAD_QUERY", "E_BAD_REQUEST"}:
                status = 400
            elif code == "E_TIMEOUT":
                status = 504
            return _error(status, code, f"{code}: {ERROR_MESSAGES[code]}")
        return web.Response(text=format_answer(result))

    return [
        web.post("/bridge/search", web_search),
        web.post("/bridge/gemini-search", gemini_search),
    ]
