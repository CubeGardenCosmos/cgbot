"""Offline fakes for the coding gateway tests: a localhost fake upstream and small builders."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from aiohttp import web

from cgbot.coding.auth import HmacSandboxAuth
from cgbot.coding.config import (
    BridgeConfig,
    CodexConfig,
    CodingConfig,
    ConcurrencyConfig,
    DockerConfig,
    HttpConfig,
)
from cgbot.coding.web import CodingWeb
from cgbot.config.settings import Secret

UPSTREAM_KEY = "upstream-SECRET-key-0001"
TOKEN_SECRET = "coding-token-secret-for-tests"


@dataclass(frozen=True, slots=True)
class Recorded:
    method: str
    raw_path: str
    headers: tuple[tuple[str, str], ...]
    body: bytes

    def header(self, name: str) -> str | None:
        for key, value in self.headers:
            if key.lower() == name.lower():
                return value
        return None


@dataclass
class FakeUpstream:
    """Localhost stand-in for the Responses API. The behaviour is selected by the path tail."""

    port: int = 0
    requests: list[Recorded] = field(default_factory=list)
    arrived: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    gate: asyncio.Event = field(default_factory=asyncio.Event)
    _runner: web.AppRunner | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    async def start(self) -> None:
        app = web.Application()
        app.router.add_route("*", "/v1", self._handle)
        app.router.add_route("*", "/v1/{tail:.*}", self._handle)
        # handler_cancellation: a dropped upstream connection cancels the handler, which lets the
        # tests observe that the gateway really closed the upstream request.
        self._runner = web.AppRunner(app, handler_cancellation=True, auto_decompress=False)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.port = int(self._runner.addresses[0][1])

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        body = await request.read()
        self.requests.append(
            Recorded(
                method=request.method,
                raw_path=request.raw_path,
                headers=tuple((k, v) for k, v in request.headers.items()),
                body=body,
            )
        )
        self.arrived.set()
        tail = request.match_info.get("tail", "")
        if tail == "echo":
            return web.Response(
                body=body,
                headers={"Content-Encoding": "zstd", "X-Upstream": "yes"},
            )
        if tail == "zstd-binary":
            # Not a real zstd frame: the gateway must pass the bytes through untouched.
            return web.Response(body=bytes(range(256)) * 512, headers={"Content-Encoding": "zstd"})
        if tail == "sse":
            resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await resp.prepare(request)
            await resp.write(b"data: one\n\n")
            await self.gate.wait()
            await resp.write(b"data: two\n\n")
            await resp.write_eof()
            return resp
        if tail.startswith("status/"):
            code = int(tail.split("/", 1)[1])
            headers = {"Retry-After": "7"} if code == 429 else {}
            return web.json_response({"upstream_status": code}, status=code, headers=headers)
        if tail == "hang":
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
        if tail == "stream-hang":
            resp = web.StreamResponse()
            await resp.prepare(request)
            await resp.write(b"data: first\n\n")
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
        if tail == "break":
            resp = web.StreamResponse(headers={"Content-Length": "1000"})
            await resp.prepare(request)
            await resp.write(b"0123456789")
            if request.transport is not None:
                request.transport.close()
            return resp
        return web.Response(text="ok")


@asynccontextmanager
async def upstream() -> AsyncIterator[FakeUpstream]:
    fake = FakeUpstream()
    await fake.start()
    try:
        yield fake
    finally:
        await fake.stop()


def closed_port() -> int:
    """A localhost port with nothing listening (bound, then released)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def coding_config(tmp_path: Path) -> CodingConfig:
    return CodingConfig(
        token_secret=Secret(TOKEN_SECRET),
        http=HttpConfig(host="127.0.0.1", port=0, public_base_url="http://127.0.0.1:0"),
        docker=DockerConfig(
            host="http://unused.invalid:2375",
            sandbox_image="cgbot-sandbox:test",
            network="test-net",
            host_data_dir=tmp_path,
            egress_proxy="",
            idle_seconds=60.0,
            container_prefix="cgbot-sbx-",
        ),
        codex=CodexConfig(
            model="m", reasoning_effort="low", appserver_port=8765, context_window=128000
        ),
        concurrency=ConcurrencyConfig(
            per_user=1,
            degraded=1,
            degrade_failures=1,
            degrade_window_seconds=1.0,
            recover_after_seconds=1.0,
        ),
        bridge=BridgeConfig(
            max_file_bytes=1024,
            chunk_bytes=256,
            temp_dir=tmp_path,
            file_ttl_seconds=60.0,
        ),
        imagegen=None,
    )


def sandbox_token(user_id: str = "10001") -> str:
    return HmacSandboxAuth(Secret(TOKEN_SECRET)).token_for(user_id)


@asynccontextmanager
async def running_web(
    tmp_path: Path,
    *,
    upstream_base_url: str,
    statuses: list[int],
    extra_routes: Iterable[web.RouteDef] = (),
) -> AsyncIterator[CodingWeb]:
    coding_web = CodingWeb(
        coding_config(tmp_path),
        upstream_base_url=upstream_base_url,
        upstream_api_key=Secret(UPSTREAM_KEY),
        status_hook=statuses.append,
        extra_routes=extra_routes,
    )
    await coding_web.start()
    try:
        yield coding_web
    finally:
        await coding_web.stop()
