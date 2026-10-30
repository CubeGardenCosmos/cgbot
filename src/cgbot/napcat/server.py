"""NapCat reverse-WebSocket server (OneBot11). NapCat is the WS client; we listen on ``path``.

Events are decoded with :func:`parse_event` and delivered in order per connection through a
queue, so a slow handler never blocks reading action responses (a handler may itself call
``send`` without deadlocking). Action responses are matched to requests by ``echo``.

Only the most recently registered connection is used for outbound calls (single bot account
in M1). One connection per ``x-self-id``: a new connection replaces the old one.

Log lines carry ids and counts only, never message bodies.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from dataclasses import dataclass, field
from http import HTTPStatus

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

from cgbot.model import InboundMessage, InputStatus, OutboundMessage, SendResult
from cgbot.napcat.codec import build_send, parse_event

log = logging.getLogger(__name__)

# Inbound frames carry event payloads; allow larger than websockets' 1 MiB default.
MAX_FRAME_BYTES = 16 * 1024 * 1024

MessageHandler = Callable[[InboundMessage], Awaitable[None]]
InputStatusHandler = Callable[[InputStatus], Awaitable[None]]
DisconnectHandler = Callable[[str], Awaitable[None]]


class NapCatError(Exception):
    """Base error for NapCat adapter calls."""


class NotConnectedError(NapCatError):
    """No NapCat connection is available, or it closed while a call was in flight."""


@dataclass(eq=False, slots=True)
class _Session:
    self_id: str
    conn: ServerConnection
    events: asyncio.Queue[InboundMessage | InputStatus | None] = field(
        default_factory=asyncio.Queue
    )
    pending: dict[str, asyncio.Future[dict[str, object]]] = field(default_factory=dict)

    def fail_pending(self, reason: str) -> None:
        for fut in self.pending.values():
            if not fut.done():
                fut.set_exception(NotConnectedError(reason))
        self.pending.clear()


class NapCatServer:
    def __init__(
        self,
        host: str,
        port: int,
        path: str = "/ws",
        token: str | None = None,
        *,
        on_message: MessageHandler,
        on_input_status: InputStatusHandler,
        on_disconnect: DisconnectHandler | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._path = path
        self._token = token or None  # empty token == no authentication configured
        self._on_message = on_message
        self._on_input_status = on_input_status
        self._on_disconnect = on_disconnect
        self._server: Server | None = None
        self._sessions: dict[str, _Session] = {}  # insertion order; last = primary
        self._background: set[asyncio.Task[None]] = set()

    @property
    def bound_port(self) -> int:
        """Actual listening port (useful when constructed with port 0)."""
        if self._server is None:
            raise NotConnectedError("server not started")
        return int(self._server.sockets[0].getsockname()[1])

    async def start(self) -> None:
        if self._server is not None:
            raise RuntimeError("NapCatServer already started")
        self._server = await serve(
            self._handle,
            self._host,
            self._port,
            process_request=self._check_handshake,
            max_size=MAX_FRAME_BYTES,
        )
        log.info(
            "NapCat reverse-WS server listening on %s:%s%s", self._host, self.bound_port, self._path
        )

    async def stop(self) -> None:
        server, self._server = self._server, None
        if server is None:
            return
        server.close(close_connections=True)
        await server.wait_closed()

    async def send(self, message: OutboundMessage, timeout_s: float = 10.0) -> SendResult:
        """Send a private/group message. Never raises for transport or NapCat failures."""
        try:
            action, params = build_send(message)
        except ValueError as exc:
            return SendResult(ok=False, error=str(exc))
        try:
            resp = await self.call(action, params, timeout_s)
        except NotConnectedError as exc:
            return SendResult(ok=False, error=f"not connected: {exc}")
        except TimeoutError:
            return SendResult(ok=False, error=f"timeout after {timeout_s}s")
        if resp.get("status") != "ok" or resp.get("retcode") != 0:
            return SendResult(
                ok=False,
                error=f"napcat status={resp.get('status')} retcode={resp.get('retcode')}",
            )
        data = resp.get("data")
        message_id = data.get("message_id") if isinstance(data, Mapping) else None
        return SendResult(ok=True, message_id=None if message_id is None else str(message_id))

    async def call(
        self,
        action: str,
        params: Mapping[str, object] | None = None,
        timeout_s: float = 10.0,
    ) -> dict[str, object]:
        """Run one OneBot action and return the raw response dict
        (``status``/``retcode``/``data``/``echo``). Raises NotConnectedError or TimeoutError."""
        session = self._primary()
        if session is None:
            raise NotConnectedError("no NapCat connection")
        echo = uuid.uuid4().hex
        fut: asyncio.Future[dict[str, object]] = asyncio.get_running_loop().create_future()
        session.pending[echo] = fut
        request = {"action": action, "params": dict(params or {}), "echo": echo}
        try:
            async with asyncio.timeout(timeout_s):
                await session.conn.send(json.dumps(request))
                return await fut
        except ConnectionClosed as exc:
            raise NotConnectedError("connection closed") from exc
        finally:
            session.pending.pop(echo, None)

    def _primary(self) -> _Session | None:
        return next(reversed(self._sessions.values()), None)

    def _check_handshake(self, conn: ServerConnection, request: Request) -> Response | None:
        if request.path.partition("?")[0] != self._path:
            return conn.respond(HTTPStatus.NOT_FOUND, "not found\n")
        if self._token is not None:
            expected = f"Bearer {self._token}".encode()
            given = request.headers.get("Authorization", "").encode()
            if not hmac.compare_digest(given, expected):
                return conn.respond(HTTPStatus.UNAUTHORIZED, "unauthorized\n")
        if not request.headers.get("x-self-id", "").isdigit():
            return conn.respond(HTTPStatus.BAD_REQUEST, "missing x-self-id\n")
        return None

    async def _handle(self, conn: ServerConnection) -> None:
        self_id = conn.request.headers["x-self-id"] if conn.request is not None else ""
        session = _Session(self_id=self_id, conn=conn)
        self._register(session)
        log.info("NapCat connected: self_id=%s", self_id)
        dispatcher = asyncio.create_task(self._dispatch(session))
        try:
            async for frame in conn:
                self._on_frame(session, frame)
        except ConnectionClosed:
            pass
        finally:
            self._unregister(session)
            session.fail_pending("connection closed")
            session.events.put_nowait(None)
            await dispatcher
            log.info("NapCat disconnected: self_id=%s", self_id)
            if self._on_disconnect is not None:
                try:
                    await self._on_disconnect(self_id)
                except Exception as exc:
                    log.error("NapCat disconnect handler failed: %s", type(exc).__name__)

    def _register(self, session: _Session) -> None:
        old = self._sessions.pop(session.self_id, None)
        self._sessions[session.self_id] = session
        if old is not None:
            log.info("NapCat connection replaced: self_id=%s", session.self_id)
            old.fail_pending("connection replaced")
            self._spawn(old.conn.close(code=1000, reason="replaced by new connection"))

    def _unregister(self, session: _Session) -> None:
        if self._sessions.get(session.self_id) is session:
            del self._sessions[session.self_id]

    def _spawn(self, coro: Coroutine[object, object, None]) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def _on_frame(self, session: _Session, frame: str | bytes) -> None:
        try:
            raw = json.loads(frame)
        except ValueError:
            log.warning("NapCat frame is not JSON: self_id=%s", session.self_id)
            return
        if not isinstance(raw, dict):
            return
        echo = raw.get("echo")
        if isinstance(echo, str) and ("status" in raw or "retcode" in raw):
            fut = session.pending.get(echo)
            if fut is not None and not fut.done():
                fut.set_result(raw)
            return
        event = parse_event(raw)
        if event is not None:
            session.events.put_nowait(event)

    async def _dispatch(self, session: _Session) -> None:
        while (event := await session.events.get()) is not None:
            try:
                if isinstance(event, InboundMessage):
                    await self._on_message(event)
                else:
                    await self._on_input_status(event)
            except Exception as exc:
                log.error("NapCat event handler failed: %s", type(exc).__name__)
