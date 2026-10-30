"""Fixtures that start in-process fake daemons (see ``fakes.py``)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from voice_fakes import FakeDaemon, Handler, Running, Starter


@pytest.fixture
def start_tcp() -> Iterator[Starter]:
    servers: list[asyncio.Server] = []

    async def start(handler: Handler, *, delay: float = 0.0) -> Running:
        daemon = FakeDaemon(handler=handler, delay=delay)
        server = await asyncio.start_server(daemon.serve, "127.0.0.1", 0)
        servers.append(server)
        port = int(server.sockets[0].getsockname()[1])
        return Running(daemon=daemon, host="127.0.0.1", port=port, unix_path=None)

    yield start
    for server in servers:
        server.close()


@pytest.fixture
def start_unix(tmp_path: Path) -> Iterator[Starter]:
    servers: list[asyncio.Server] = []

    async def start(handler: Handler, *, delay: float = 0.0) -> Running:
        daemon = FakeDaemon(handler=handler, delay=delay)
        path = tmp_path / f"d{len(servers)}.sock"
        server = await asyncio.start_unix_server(daemon.serve, path=str(path))
        servers.append(server)
        return Running(daemon=daemon, host=None, port=None, unix_path=str(path))

    yield start
    for server in servers:
        server.close()
