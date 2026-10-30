"""httpx client for one external ompweb server (R5.7 omp_* tools).

Ports the endpoint sequence of the old aiohttp plugin: auth detection, cookie login, session
verification, JSON calls, and the SSE event stream. Failures map to fixed user-facing messages.
Transport and HTTP exception text is never forwarded, because it can contain URLs, bodies, or
the password.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

from cgbot.omp.store import Machine

HTTP_TIMEOUT_SECONDS = 30.0
BODY_READ_LIMIT = 2048
ERROR_TEXT_LIMIT = 300
REDACTED = "[REDACTED]"
ALIAS_MAX_LENGTH = 64
HOST_MAX_LENGTH = 253

MSG_UNSUPPORTED = "不是支持的 ompweb（需要 @kahme247/ompweb 的密码登录版本）。"
MSG_LOGIN_REJECTED = "登录被拒：请检查密码。"
MSG_CONN_REFUSED = "ompweb 拒绝连接。"
MSG_CONN_TIMEOUT = "连接 ompweb 超时。"
MSG_CONN_FAILED = "无法连接 ompweb。"
MSG_REQUEST_TIMEOUT = "ompweb 请求超时。"
MSG_BAD_RESPONSE = "ompweb 响应无效。"
MSG_OP_FAILED = "ompweb 未能执行该操作。"
MSG_STREAM_UNAVAILABLE = "事件流暂不可用。"
MSG_SESSION_EXPIRED = "ompweb 登录失效。"
MSG_BAD_HOST = "主机地址无效。"
MSG_BAD_ENDPOINT = "请提供有效主机、端口和别名。"


class OmpError(Exception):
    """A failure with a user-safe message. ``str(err)`` is always safe to show."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


ClientFactory = Callable[[], httpx.AsyncClient]


def default_client_factory() -> httpx.AsyncClient:
    return httpx.AsyncClient(follow_redirects=False, timeout=HTTP_TIMEOUT_SECONDS)


def validate_alias(alias: str) -> str:
    value = alias.strip()
    if not value or len(value) > ALIAS_MAX_LENGTH or any(ch.isspace() for ch in value):
        raise OmpError(MSG_BAD_ENDPOINT)
    if any(not ch.isprintable() or ch in "/\\" for ch in value):
        raise OmpError(MSG_BAD_ENDPOINT)
    return value


def validate_host(host: str) -> str:
    """Accept a DNS name, IPv4, or IPv6 literal. Reject characters that change the URL shape."""
    value = host.strip()
    if not value or len(value) > HOST_MAX_LENGTH:
        raise OmpError(MSG_BAD_HOST)
    if any(ch in "/@?#[]\\" or ch.isspace() or not ch.isprintable() for ch in value):
        raise OmpError(MSG_BAD_HOST)
    if ":" in value:
        try:
            ipaddress.IPv6Address(value)
        except ValueError as exc:
            raise OmpError(MSG_BAD_HOST) from exc
    return value


def validate_port(port: int) -> int:
    if not 1 <= port <= 65535:
        raise OmpError(MSG_BAD_ENDPOINT)
    return port


def base_url(host: str, port: int) -> str:
    """Build the origin URL; bracket IPv6 literals."""
    if ":" in host:
        return f"http://[{host}]:{port}"
    return f"http://{host}:{port}"


def redact(text: str, secrets: list[str]) -> str:
    """Replace every secret occurrence, both raw and JSON-escaped (for serialized output)."""
    out = text
    for secret in secrets:
        if not secret:
            continue
        for variant in {secret, json.dumps(secret, ensure_ascii=False)[1:-1]}:
            out = out.replace(variant, REDACTED)
    return out


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _extract_error(body: str) -> str:
    try:
        data = json.loads(body)
    except ValueError:
        return body.strip()
    if isinstance(data, dict):
        for key in ("error", "message"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _transport_error(exc: httpx.HTTPError) -> OmpError:
    if isinstance(exc, httpx.ConnectTimeout):
        return OmpError(MSG_CONN_TIMEOUT)
    if isinstance(exc, httpx.ConnectError):
        refused = "refused" in str(exc).lower()
        return OmpError(MSG_CONN_REFUSED if refused else MSG_CONN_FAILED)
    if isinstance(exc, httpx.TimeoutException):
        return OmpError(MSG_REQUEST_TIMEOUT)
    return OmpError(MSG_CONN_FAILED)


@dataclass(slots=True)
class OmpClient:
    """One ompweb endpoint. Owns an httpx client; call :meth:`aclose` when dropping it."""

    machine: Machine
    http: httpx.AsyncClient
    passwords: list[str] = field(default_factory=list)
    auth: str = "cookie"

    @property
    def origin(self) -> str:
        return base_url(self.machine.host, self.machine.port)

    async def aclose(self) -> None:
        await self.http.aclose()

    def _url(self, path: str) -> str:
        return f"{self.origin}{path}"

    def _scrub(self, text: str) -> str:
        return redact(text, [self.machine.password, *self.passwords])

    async def _send(
        self,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,
        params: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        try:
            async with asyncio.timeout(HTTP_TIMEOUT_SECONDS):
                return await self.http.request(
                    method, self._url(path), json=json_body, params=params
                )
        except TimeoutError as exc:
            raise OmpError(MSG_REQUEST_TIMEOUT) from exc
        except httpx.HTTPError as exc:
            raise _transport_error(exc) from exc

    async def _http_failure(self, response: httpx.Response) -> OmpError:
        detail = _extract_error(response.text[:BODY_READ_LIMIT])
        text = _clip(self._scrub(detail), ERROR_TEXT_LIMIT) if detail else ""
        suffix = f"：{text}" if text else ""
        return OmpError(f"ompweb 请求失败（HTTP {response.status_code}）{suffix}")

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise OmpError(MSG_BAD_RESPONSE) from exc
        if not isinstance(data, dict):
            raise OmpError(MSG_BAD_RESPONSE)
        if data.get("success") is False:
            raise OmpError(MSG_OP_FAILED)
        return data

    async def _call(
        self,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,
        params: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """JSON call. A 401 on a cookie server re-logs in once, then retries."""
        response = await self._send(method, path, json_body=json_body, params=params)
        if response.status_code == 401 and self.auth == "cookie":
            await self.login()
            response = await self._send(method, path, json_body=json_body, params=params)
        if response.status_code == 401:
            raise OmpError(MSG_SESSION_EXPIRED)
        if response.status_code != 200:
            raise await self._http_failure(response)
        return self._json(response)

    async def detect_auth(self) -> str:
        """Return ``"none"`` or ``"cookie"``; raise OmpError when the server is not supported.

        Cookies are cleared first and redirects are never followed, so a redirect to a login
        page cannot be mistaken for an open server.
        """
        self.http.cookies.clear()
        response = await self._send("GET", "/api/home")
        if "basic" in response.headers.get("www-authenticate", "").lower():
            raise OmpError(MSG_UNSUPPORTED)
        if response.status_code == 200:
            return "none"
        if response.status_code == 401:
            return "cookie"
        raise OmpError(MSG_UNSUPPORTED)

    async def connect(self) -> str:
        """Detect the auth mode, log in if needed, and verify the server. Returns the mode."""
        auth = await self.detect_auth()
        self.auth = auth
        if auth == "cookie":
            await self.login()
        else:
            await self.verify_sessions()
        return auth

    async def login(self) -> None:
        """Cookie login with the saved password, then verify with ``/api/sessions``."""
        self.http.cookies.clear()
        response = await self._send(
            "POST", "/api/web-auth/session", json_body={"password": self.machine.password}
        )
        if response.status_code in (401, 403):
            raise OmpError(MSG_LOGIN_REJECTED)
        if response.status_code == 404:
            raise OmpError(MSG_UNSUPPORTED)
        if response.status_code != 200:
            raise await self._http_failure(response)
        await self.verify_sessions()

    async def verify_sessions(self) -> dict[str, Any]:
        """``GET /api/sessions``; the reply must carry the three documented lists."""
        response = await self._send("GET", "/api/sessions")
        if response.status_code in (401, 403):
            raise OmpError(MSG_LOGIN_REJECTED)
        if response.status_code == 404:
            raise OmpError(MSG_UNSUPPORTED)
        if response.status_code != 200:
            raise await self._http_failure(response)
        data = self._json(response)
        for key in ("sessions", "runningSessionIds", "runningSessions"):
            if not isinstance(data.get(key), list):
                raise OmpError(MSG_UNSUPPORTED)
        return data

    async def get(self, path: str, params: Mapping[str, str] | None = None) -> dict[str, Any]:
        return await self._call("GET", path, params=params)

    async def post(self, path: str, body: Any | None = None) -> dict[str, Any]:
        return await self._call("POST", path, json_body=body)

    async def _open_stream(self, path: str) -> httpx.Response:
        request = self.http.build_request(
            "GET",
            self._url(path),
            headers={"Accept": "text/event-stream"},
            timeout=httpx.Timeout(HTTP_TIMEOUT_SECONDS, read=None),
        )
        try:
            return await self.http.send(request, stream=True)
        except httpx.HTTPError as exc:
            raise _transport_error(exc) from exc

    async def events(self, session_id: str) -> AsyncIterator[dict[str, Any]]:
        """Yield JSON objects from ``/api/agent/{id}/events`` (SSE ``data:`` blocks)."""
        path = agent_path(session_id) + "/events"
        response = await self._open_stream(path)
        try:
            if response.status_code == 401 and self.auth == "cookie":
                await response.aclose()
                await self.login()
                response = await self._open_stream(path)
            if response.status_code != 200:
                raise OmpError(MSG_STREAM_UNAVAILABLE)
            data_lines: list[str] = []
            async for line in response.aiter_lines():
                if line == "":
                    if data_lines:
                        payload = "\n".join(data_lines)
                        data_lines = []
                        try:
                            event = json.loads(payload)
                        except ValueError:
                            continue
                        if isinstance(event, dict):
                            yield event
                elif line.startswith("data:"):
                    value = line[5:]
                    data_lines.append(value[1:] if value.startswith(" ") else value)
        except httpx.HTTPError as exc:
            raise OmpError(MSG_STREAM_UNAVAILABLE) from exc
        finally:
            await response.aclose()


def session_path(session_id: str) -> str:
    return f"/api/sessions/{quote(session_id, safe='')}"


def agent_path(session_id: str) -> str:
    return f"/api/agent/{quote(session_id, safe='')}"


class ClientPool:
    """One OmpClient per (owner, alias). Replacing or dropping an alias closes the old client."""

    def __init__(self, factory: ClientFactory) -> None:
        self._factory = factory
        self._clients: dict[tuple[str, str], OmpClient] = {}

    def build(self, machine: Machine, passwords: list[str]) -> OmpClient:
        return OmpClient(machine=machine, http=self._factory(), passwords=passwords)

    def get(self, owner: str, alias: str) -> OmpClient | None:
        return self._clients.get((owner, alias))

    async def put(self, owner: str, alias: str, client: OmpClient) -> None:
        previous = self._clients.pop((owner, alias), None)
        self._clients[(owner, alias)] = client
        if previous is not None and previous is not client:
            await previous.aclose()

    async def drop(self, owner: str, alias: str) -> None:
        client = self._clients.pop((owner, alias), None)
        if client is not None:
            await client.aclose()

    async def close(self) -> None:
        clients = list(self._clients.values())
        self._clients.clear()
        for client in clients:
            await client.aclose()
