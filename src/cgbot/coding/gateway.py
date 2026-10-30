"""Sandbox -> upstream Responses gateway: a thin pass-through with no protocol translation.

Sandboxes call ``/v1/...`` with their HMAC bearer token (see :mod:`cgbot.coding.auth`). The gateway
checks that token, replaces it with the real upstream API key and forwards the request to the fixed
upstream base URL. The sandbox never sees the upstream key and can never choose the destination.
Responses are streamed chunk by chunk (SSE is not buffered); request and response bodies are passed
through byte for byte, including ``Content-Encoding`` and ``Content-Length``.
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from typing import Final
from urllib.parse import unquote

import aiohttp
from aiohttp import web
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

from cgbot.coding.auth import bearer_token
from cgbot.coding.contracts import SandboxAuth, UpstreamStatusHook
from cgbot.config.settings import Secret

log = logging.getLogger(__name__)

GATEWAY_PREFIX: Final = "/v1"
CONNECT_TIMEOUT_SECONDS: Final = 30.0
# Maximum silence between two upstream reads. Reasoning models can pause for minutes while thinking,
# so this must be generous; it only guards against a dead connection.
IDLE_TIMEOUT_SECONDS: Final = 600.0

_HOP_BY_HOP: Final = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
# Never forwarded from the sandbox: the sandbox token (replaced by the real key), the Host of the
# gateway (aiohttp sets the upstream one) and Expect (the client already sends the body).
_REQUEST_DROP: Final = frozenset({"host", "authorization", "expect"})
_UNSAFE_PATH: Final = re.compile(r"[\x00-\x1f\x7f\\]")


def validate_upstream_base_url(url: str) -> str:
    """Return the upstream base URL without a trailing slash; reject anything not plain http(s)."""
    parsed = URL(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.host
        or parsed.user is not None
        or parsed.password is not None
        or parsed.query_string
        or parsed.fragment
    ):
        raise ValueError("upstream base URL must be a plain http(s) URL without credentials")
    return url.rstrip("/")


def upstream_suffix(raw_target: str) -> str | None:
    """Map a raw request target under ``/v1`` to the upstream path+query, or None to reject.

    Rejects absolute-form targets, dot segments after percent-decoding, backslashes, control
    characters, and fragments. Everything else is forwarded exactly as the client sent it.
    """
    if "#" in raw_target:
        return None
    path, sep, query = raw_target.partition("?")
    if path != GATEWAY_PREFIX and not path.startswith(GATEWAY_PREFIX + "/"):
        return None
    tail = path[len(GATEWAY_PREFIX) :]
    decoded = unquote(tail)
    if _UNSAFE_PATH.search(tail) or _UNSAFE_PATH.search(decoded):
        return None
    if any(segment in {".", ".."} for segment in decoded.split("/")):
        return None
    return tail + (sep + query if query else "")


def _hop_headers(headers: CIMultiDictProxy[str]) -> frozenset[str]:
    """Standard hop-by-hop names plus any extra names listed in ``Connection``."""
    names = set(_HOP_BY_HOP)
    for value in headers.getall("Connection", []):
        names.update(token.strip().lower() for token in value.split(",") if token.strip())
    return frozenset(names)


def _forward_request_headers(headers: CIMultiDictProxy[str], api_key: str) -> CIMultiDict[str]:
    drop = _hop_headers(headers) | _REQUEST_DROP
    out: CIMultiDict[str] = CIMultiDict()
    for name, value in headers.items():
        if name.lower() not in drop:
            out.add(name, value)
    out["Authorization"] = f"Bearer {api_key}"
    return out


def _forward_response_headers(headers: CIMultiDictProxy[str]) -> Iterable[tuple[str, str]]:
    drop = _hop_headers(headers)
    return [(name, value) for name, value in headers.items() if name.lower() not in drop]


def _json_error(status: int, message: str) -> web.Response:
    return web.json_response({"error": message}, status=status)


@dataclass(slots=True)
class _UploadState:
    client_failed: bool = False


async def _request_body(request: web.Request, state: _UploadState) -> AsyncIterator[bytes]:
    try:
        async for chunk in request.content.iter_any():
            yield chunk
    except Exception:
        state.client_failed = True
        raise


class ResponsesGateway:
    """aiohttp handlers for ``/v1/*``. Fixed upstream; sandbox-authenticated; streaming."""

    def __init__(
        self,
        *,
        auth: SandboxAuth,
        session: aiohttp.ClientSession,
        upstream_base_url: str,
        upstream_api_key: Secret,
        status_hook: UpstreamStatusHook,
    ) -> None:
        self._auth = auth
        self._session = session
        self._base = validate_upstream_base_url(upstream_base_url)
        self._api_key = upstream_api_key
        self._status_hook = status_hook

    def routes(self) -> list[web.RouteDef]:
        return [
            web.route("*", GATEWAY_PREFIX, self.handle),
            web.route("*", GATEWAY_PREFIX + "/{tail:.*}", self.handle),
        ]

    async def handle(self, request: web.Request) -> web.StreamResponse:
        token = bearer_token(request.headers.get("Authorization"))
        if token is None or self._auth.resolve(token) is None:
            return _json_error(401, "unauthorized")
        suffix = upstream_suffix(request.raw_path)
        if suffix is None:
            return _json_error(404, "not found")

        upload = _UploadState()
        body = _request_body(request, upload) if request.body_exists else None
        headers = _forward_request_headers(request.headers, self._api_key.reveal())
        try:
            upstream = await self._session.request(
                request.method,
                URL(self._base + suffix, encoded=True),
                headers=headers,
                data=body,
                allow_redirects=False,
            )
        except (TimeoutError, aiohttp.ClientError) as exc:
            if upload.client_failed:
                return _json_error(400, "request body interrupted")
            self._report_status(0)
            log.warning("upstream unreachable (%s)", type(exc).__name__)
            return _json_error(502, "upstream unavailable")

        self._report_status(upstream.status)
        response = web.StreamResponse(status=upstream.status, reason=upstream.reason or "")
        for name, value in _forward_response_headers(upstream.headers):
            response.headers.add(name, value)
        try:
            await response.prepare(request)
            async for chunk in upstream.content.iter_any():
                await response.write(chunk)
            await response.write_eof()
        except (TimeoutError, ConnectionError, aiohttp.ClientError):
            # Client went away, or upstream broke mid-stream. Headers may already be sent, so the
            # only honest signal left is to drop the connection instead of ending the body cleanly.
            if request.transport is not None:
                request.transport.close()
        finally:
            _release(upstream)
        return response

    def _report_status(self, status: int) -> None:
        try:
            self._status_hook(status)
        except Exception as exc:
            log.warning("gateway status hook failed (%s)", type(exc).__name__)


def _release(upstream: aiohttp.ClientResponse) -> None:
    if upstream.content.is_eof():
        upstream.release()
    else:
        upstream.close()
