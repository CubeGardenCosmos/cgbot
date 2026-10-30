"""Scripted stand-in for ``codex app-server`` over a real localhost websocket.

Tests drive :mod:`cgbot.coding.appserver` against this server, so framing, headers and
connection-loss behaviour are exercised on a real socket without the Codex binary.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

TOKEN = "tok-test-0123456789"  # test-only capability token


class NoReply:
    """Return from a handler to leave the request unanswered (exercises timeouts)."""


NO_REPLY = NoReply()


@dataclass(frozen=True, slots=True)
class RpcFail(Exception):
    code: int
    message: str


Handler = Callable[[dict[str, Any]], Any]


class FakeCodex:
    def __init__(self, token: str = TOKEN) -> None:
        self.token = token
        self.handlers: dict[str, Handler] = {
            "initialize": lambda _p: {
                "userAgent": "fake/0",
                "codexHome": "/fake",
                "platformFamily": "unix",
                "platformOs": "linux",
            },
        }
        # Requests and notifications received from the client, in arrival order.
        self.calls: list[tuple[str, dict[str, Any]]] = []
        # Raw messages the client sent back (answers to server requests, ids preserved).
        self.answers: list[dict[str, Any]] = []
        self.handshake_headers: list[Headers] = []
        self._conns: list[ServerConnection] = []
        self._server: Server | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self.port = 0

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    async def start(self) -> None:
        self._server = await serve(
            self._handle,
            "127.0.0.1",
            0,
            process_request=self._check_auth,
            max_size=16 * 1024 * 1024,
        )
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for ws in list(self._conns):
            await ws.close()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    def _check_auth(self, connection: ServerConnection, request: Request) -> Response | None:
        if request.headers.get("Authorization") != f"Bearer {self.token}":
            return connection.respond(HTTPStatus.UNAUTHORIZED, "unauthorized\n")
        return None

    async def _handle(self, ws: ServerConnection) -> None:
        self._conns.append(ws)
        assert ws.request is not None
        self.handshake_headers.append(ws.request.headers)
        try:
            async for raw in ws:
                msg = json.loads(raw)
                if "method" in msg:
                    self.calls.append((msg["method"], msg.get("params") or {}))
                    if "id" in msg:
                        # Replies run concurrently so tests can answer requests out of order.
                        task = asyncio.create_task(self._reply(ws, msg))
                        self._tasks.add(task)
                        task.add_done_callback(self._tasks.discard)
                else:
                    self.answers.append(msg)
        except ConnectionClosed:
            pass

    async def _reply(self, ws: ServerConnection, msg: dict[str, Any]) -> None:
        handler = self.handlers.get(msg["method"])
        if handler is None:
            await ws.send(
                json.dumps(
                    {
                        "id": msg["id"],
                        "error": {"code": -32601, "message": f"no fake for {msg['method']}"},
                    }
                )
            )
            return
        try:
            result = handler(msg.get("params") or {})
            if inspect.isawaitable(result):
                result = await result
        except RpcFail as exc:
            await ws.send(
                json.dumps({"id": msg["id"], "error": {"code": exc.code, "message": exc.message}})
            )
            return
        if isinstance(result, NoReply):
            return
        await ws.send(json.dumps({"id": msg["id"], "result": result}))

    async def push(self, message: dict[str, Any]) -> None:
        """Send a raw message (notification or server request) to every open connection."""
        text = json.dumps(message)
        for ws in list(self._conns):
            await ws.send(text)

    async def drop(self) -> None:
        """Close every open connection from the server side."""
        for ws in list(self._conns):
            await ws.close(code=1011, reason="fake drop")

    def methods(self) -> list[str]:
        return [method for method, _ in self.calls]

    def params_of(self, method: str) -> list[dict[str, Any]]:
        return [params for name, params in self.calls if name == method]
