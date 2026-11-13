"""Minimal Docker Engine API client for sandbox lifecycle (M5, slice B).

Talks only to the docker-socket-proxy allowlist (``caged/docker-proxy/haproxy.cfg`` in the
deployment). The client additionally refuses to send any request whose (method, path) is not one
of the routes below, so a future edit cannot silently widen what the bot asks of the daemon.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx

_NAME = r"[a-zA-Z0-9][a-zA-Z0-9_.-]*"

# (method, path regex) pairs this client uses. Every entry must match a proxy allowlist rule;
# tests assert that subset relation. No logs/wait/images/volumes endpoints.
_ROUTES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("GET", re.compile(r"/containers/json")),
    ("GET", re.compile(rf"/containers/{_NAME}/json")),
    ("POST", re.compile(r"/containers/create")),
    ("POST", re.compile(rf"/containers/{_NAME}/start")),
    ("POST", re.compile(rf"/containers/{_NAME}/stop")),
    ("DELETE", re.compile(rf"/containers/{_NAME}")),
    ("POST", re.compile(rf"/containers/{_NAME}/exec")),
    ("POST", re.compile(r"/exec/[a-zA-Z0-9]+/start")),
    ("GET", re.compile(r"/exec/[a-zA-Z0-9]+/json")),
)
_REF = re.compile(_NAME)
_EXEC_ID = re.compile(r"[a-zA-Z0-9]+")


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


@dataclass(frozen=True, slots=True)
class ExecResult:
    """One finished exec: inspect's ExitCode plus the demultiplexed streams."""

    exit_code: int
    stdout: bytes
    stderr: bytes


def _demux(raw: bytes) -> tuple[bytes, bytes]:
    """Split a Docker multiplexed stream.

    Frame = kind (1=stdout, 2=stderr), 3 spare bytes, 4-byte big-endian size.
    """
    out = bytearray()
    err = bytearray()
    pos = 0
    while pos < len(raw):
        if pos + 8 > len(raw):
            raise DockerError(0, "exec output frame truncated")
        kind = raw[pos]
        size = int.from_bytes(raw[pos + 4 : pos + 8], "big")
        body = raw[pos + 8 : pos + 8 + size]
        if len(body) != size:
            raise DockerError(0, "exec output frame truncated")
        if kind == 1:
            out += body
        elif kind == 2:
            err += body
        else:
            raise DockerError(0, f"unknown exec output stream {kind}")
        pos += 8 + size
    return bytes(out), bytes(err)


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

    async def exec_run(self, container: str, cmd: Sequence[str], *, timeout_s: float) -> ExecResult:
        """Run ``cmd`` once in ``container``: create exec, start it, inspect ExitCode.

        Only the three exec routes are used; container logs and wait are never called.
        """
        created = await self._call(
            "POST",
            f"/containers/{_check_ref(container)}/exec",
            body={"AttachStdout": True, "AttachStderr": True, "Cmd": list(cmd)},
            ok=(201,),
        )
        payload = created.json()
        exec_id = payload.get("Id") if isinstance(payload, dict) else None
        if not isinstance(exec_id, str) or not _EXEC_ID.fullmatch(exec_id):
            raise DockerError(0, "exec create returned no valid id")
        started = await self._call(
            "POST",
            f"/exec/{exec_id}/start",
            body={"Detach": False, "Tty": False},
            ok=(200,),
            timeout_s=timeout_s,
        )
        stdout, stderr = _demux(started.content)
        inspected = await self._call("GET", f"/exec/{exec_id}/json", ok=(200,))
        payload = inspected.json()
        exit_code = payload.get("ExitCode") if isinstance(payload, dict) else None
        if not isinstance(exit_code, int) or isinstance(exit_code, bool):
            raise DockerError(0, "exec inspect returned no exit code")
        return ExecResult(exit_code=exit_code, stdout=stdout, stderr=stderr)

    async def _call(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        ok: tuple[int, ...] = (200, 201, 204),
        timeout_s: float | None = None,
    ) -> httpx.Response:
        if not any(m == method and rx.fullmatch(path) for m, rx in _ROUTES):
            raise ForbiddenRoute(f"{method} {path} is not a sandbox lifecycle route")
        request_timeout = httpx.USE_CLIENT_DEFAULT if timeout_s is None else timeout_s
        response = await self._http.request(
            method, path, params=params, json=body, timeout=request_timeout
        )
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
