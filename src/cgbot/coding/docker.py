"""Minimal Docker Engine API client for sandbox lifecycle (M5, slice B).

Talks only to the docker-socket-proxy allowlist (``caged/docker-proxy/haproxy.cfg`` in the
deployment). The client additionally refuses to send any request whose (method, path) is not one
of the routes below, so a future edit cannot silently widen what the bot asks of the daemon.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

_NAME = r"[a-zA-Z0-9][a-zA-Z0-9_.-]*"

# (method, path regex) pairs this client uses. Every entry must match a proxy allowlist rule;
# tests assert that subset relation. No logs/wait/images/volumes/exec endpoints.
_ROUTES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("GET", re.compile(r"/containers/json")),
    ("GET", re.compile(rf"/containers/{_NAME}/json")),
    ("POST", re.compile(r"/containers/create")),
    ("POST", re.compile(rf"/containers/{_NAME}/start")),
    ("POST", re.compile(rf"/containers/{_NAME}/stop")),
    ("DELETE", re.compile(rf"/containers/{_NAME}")),
)
_REF = re.compile(_NAME)


class DockerError(Exception):
    """Base error for a non-success Docker API response."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"docker API {status}: {message}")
        self.status = status
        self.message = message


class NotFound(DockerError):
    """404: no such container."""


class Conflict(DockerError):
    """409: name already in use or container in an incompatible state."""


class ApiError(DockerError):
    """Any other error status, including proxy denials (403)."""


class ForbiddenRoute(Exception):
    """Client-side guard: a request outside the sandbox lifecycle routes was attempted."""


def _error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(body, dict) and isinstance(body.get("message"), str):
        return body["message"][:200]
    return f"status {response.status_code}"


class DockerClient:
    """Typed wrapper over the Engine API. ``http`` must already carry the proxy base URL."""

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def aclose(self) -> None:
        await self._http.aclose()

    async def list_containers(self, *, label: str) -> list[dict[str, Any]]:
        """All containers (including stopped) carrying ``label`` (``key=value``)."""
        params = {"all": "true", "filters": json.dumps({"label": [label]})}
        response = await self._call("GET", "/containers/json", params=params)
        return list(response.json())

    async def inspect_container(self, ref: str) -> dict[str, Any]:
        response = await self._call("GET", f"/containers/{_check_ref(ref)}/json")
        return dict(response.json())

    async def create_container(self, name: str, spec: dict[str, Any]) -> str:
        """Create a container named ``name`` from the Engine API create body; returns its id."""
        response = await self._call(
            "POST", "/containers/create", params={"name": _check_ref(name)}, body=spec
        )
        return str(response.json()["Id"])

    async def start_container(self, ref: str) -> None:
        # 304 = already started, which is the state we want.
        await self._call("POST", f"/containers/{_check_ref(ref)}/start", ok=(204, 304))

    async def stop_container(self, ref: str, *, timeout_seconds: int = 10) -> None:
        # 304 = already stopped. 404 surfaces as NotFound for the caller to decide.
        await self._call(
            "POST",
            f"/containers/{_check_ref(ref)}/stop",
            params={"t": str(timeout_seconds)},
            ok=(204, 304),
        )

    async def remove_container(self, ref: str, *, force: bool = False) -> None:
        params = {"force": "true" if force else "false"}
        await self._call("DELETE", f"/containers/{_check_ref(ref)}", params=params, ok=(204,))

    async def _call(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        ok: tuple[int, ...] = (200, 201, 204),
    ) -> httpx.Response:
        if not any(m == method and rx.fullmatch(path) for m, rx in _ROUTES):
            raise ForbiddenRoute(f"{method} {path} is not a sandbox lifecycle route")
        response = await self._http.request(method, path, params=params, json=body)
        if response.status_code in ok:
            return response
        message = _error_message(response)
        if response.status_code == 404:
            raise NotFound(404, message)
        if response.status_code == 409:
            raise Conflict(409, message)
        raise ApiError(response.status_code, message)


def _check_ref(ref: str) -> str:
    if not _REF.fullmatch(ref):
        raise ValueError("invalid docker container reference")
    return ref
