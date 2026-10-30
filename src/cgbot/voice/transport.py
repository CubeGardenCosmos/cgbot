"""Endpoint validation and a small connection pool for one-request-at-a-time JSON services.

Connections are reused across requests (the daemons keep them open). A connection carries at
most one in-flight request because the protocol has no request IDs. Any failure discards that
connection; the next request opens a fresh one, so a daemon restart costs one failed call
rather than a desynchronised stream. Requests are not retried automatically: whether to repeat
a call is the caller's decision.

Deadline semantics: ``call(timeout=...)`` bounds the whole operation, including waiting for a
free connection, connecting, writing, and reading the response.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from cgbot.voice.errors import VoiceConnectionError, VoiceTimeoutError
from cgbot.voice.wire import decode_response, encode_frame, read_frame

log = logging.getLogger("cgbot.voice")

_PORT_RANGE: Final = range(1, 65536)


def is_private_host(host: str) -> bool:
    """True for loopback or private IP literals. Hostnames and public addresses are refused."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return (address.is_loopback or address.is_private) and not address.is_unspecified


@dataclass(frozen=True, slots=True)
class Endpoint:
    """Exactly one of ``unix_path`` or ``host``/``port``.

    TCP hosts must be loopback or private IP literals (the services are LAN/UDS only, §11.9).
    """

    unix_path: str | None = None
    host: str | None = None
    port: int | None = None

    def __post_init__(self) -> None:
        if (self.unix_path is None) == (self.host is None):
            raise ValueError("endpoint needs exactly one of unix_path or host")
        if self.unix_path is not None:
            if self.port is not None or not self.unix_path.startswith("/"):
                raise ValueError("unix_path must be absolute and carry no port")
            return
        assert self.host is not None
        if self.port is None or self.port not in _PORT_RANGE:
            raise ValueError("tcp endpoint needs a port in 1..65535")
        if not is_private_host(self.host):
            raise ValueError("tcp host must be a loopback or private IP literal")

    def describe(self) -> str:
        """Address for logs and error messages (no secrets are ever stored here)."""
        if self.unix_path is not None:
            return f"unix:{self.unix_path}"
        return f"tcp:{self.host}:{self.port}"


@dataclass(eq=False, slots=True)
class _Connection:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter


class JsonServiceClient:
    """Pool of persistent connections to one daemon, bounded by ``max_connections``."""

    def __init__(
        self,
        name: str,
        endpoint: Endpoint,
        *,
        connect_timeout: float,
        max_connections: int,
    ) -> None:
        if connect_timeout <= 0:
            raise ValueError("connect_timeout must be positive")
        if not 1 <= max_connections <= 8:
            raise ValueError("max_connections must be in 1..8")
        self._name = name
        self._endpoint = endpoint
        self._connect_timeout = connect_timeout
        self._slots = asyncio.Semaphore(max_connections)
        self._idle: list[_Connection] = []
        self._closed = False

    @property
    def name(self) -> str:
        return self._name

    # The whole-call deadline is the contract (slot wait + connect + I/O), not an async hint.
    async def call(
        self,
        payload: Mapping[str, object],
        *,
        timeout: float,  # noqa: ASYNC109
    ) -> dict[str, object]:
        """Send one request and return its decoded response object (status not inspected)."""
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        frame = encode_frame(payload)  # validates size before any connection is used
        action = str(payload.get("action", "?"))
        try:
            async with asyncio.timeout(timeout):
                async with self._slots:
                    conn = await self._checkout()
                    try:
                        body = await self._exchange(conn, frame)
                    except BaseException:
                        conn.writer.close()
                        raise
                    self._checkin(conn)
        except TimeoutError as exc:
            log.warning("%s %s timed out after %.1fs", self._name, action, timeout)
            raise VoiceTimeoutError(f"{self._name} {action} timed out") from exc
        log.debug("%s %s answered (%d bytes)", self._name, action, len(body))
        return decode_response(body)

    async def aclose(self) -> None:
        """Close idle connections. Connections still in flight close when their call ends."""
        self._closed = True
        idle, self._idle = self._idle, []
        for conn in idle:
            conn.writer.close()
        for conn in idle:
            try:
                await conn.writer.wait_closed()
            except OSError:
                pass

    async def _checkout(self) -> _Connection:
        if self._closed:
            raise VoiceConnectionError(f"{self._name} client is closed")
        if self._idle:
            return self._idle.pop()
        try:
            async with asyncio.timeout(self._connect_timeout):
                reader, writer = await self._open()
        except TimeoutError as exc:
            raise VoiceTimeoutError(f"{self._name} connect timed out") from exc
        except OSError as exc:
            raise VoiceConnectionError(
                f"{self._name} connect to {self._endpoint.describe()} failed: "
                f"{exc.strerror or type(exc).__name__}"
            ) from exc
        return _Connection(reader, writer)

    async def _open(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if self._endpoint.unix_path is not None:
            return await asyncio.open_unix_connection(self._endpoint.unix_path)
        assert self._endpoint.host is not None and self._endpoint.port is not None
        return await asyncio.open_connection(self._endpoint.host, self._endpoint.port)

    async def _exchange(self, conn: _Connection, frame: bytes) -> bytes:
        try:
            conn.writer.write(frame)
            await conn.writer.drain()
        except OSError as exc:
            raise VoiceConnectionError(
                f"{self._name} write failed: {exc.strerror or type(exc).__name__}"
            ) from exc
        return await read_frame(conn.reader)

    def _checkin(self, conn: _Connection) -> None:
        if self._closed:
            conn.writer.close()
        else:
            self._idle.append(conn)
