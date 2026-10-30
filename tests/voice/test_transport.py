from __future__ import annotations

import asyncio
import socket

import pytest
from voice_fakes import Starter

from cgbot.voice.errors import (
    VoiceConnectionError,
    VoiceInputError,
    VoiceProtocolError,
    VoiceTimeoutError,
)
from cgbot.voice.transport import Endpoint, JsonServiceClient, is_private_host


def _client(endpoint: Endpoint, *, max_connections: int = 1) -> JsonServiceClient:
    return JsonServiceClient("svc", endpoint, connect_timeout=2.0, max_connections=max_connections)


def test_endpoint_requires_exactly_one_target() -> None:
    with pytest.raises(ValueError):
        Endpoint()
    with pytest.raises(ValueError):
        Endpoint(unix_path="/tmp/a.sock", host="127.0.0.1", port=1)


def test_unix_path_must_be_absolute_and_portless() -> None:
    with pytest.raises(ValueError):
        Endpoint(unix_path="relative.sock")
    with pytest.raises(ValueError):
        Endpoint(unix_path="/tmp/a.sock", port=10095)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "192.168.1.19", "10.0.0.5"])
def test_loopback_and_private_hosts_accepted(host: str) -> None:
    assert is_private_host(host)
    assert Endpoint(host=host, port=10095).describe() == f"tcp:{host}:10095"


@pytest.mark.parametrize("host", ["8.8.8.8", "example.com", "localhost", "0.0.0.0"])
def test_public_or_named_hosts_refused(host: str) -> None:
    assert not is_private_host(host)
    with pytest.raises(ValueError):
        Endpoint(host=host, port=10095)


@pytest.mark.parametrize("port", [None, 0, 65536])
def test_tcp_port_must_be_in_range(port: int | None) -> None:
    with pytest.raises(ValueError):
        Endpoint(host="127.0.0.1", port=port)


def test_client_parameter_bounds() -> None:
    endpoint = Endpoint(host="127.0.0.1", port=1)
    with pytest.raises(ValueError):
        JsonServiceClient("svc", endpoint, connect_timeout=0, max_connections=1)
    with pytest.raises(ValueError):
        JsonServiceClient("svc", endpoint, connect_timeout=1, max_connections=0)
    with pytest.raises(ValueError):
        JsonServiceClient("svc", endpoint, connect_timeout=1, max_connections=9)


async def test_tcp_round_trip(start_tcp: Starter) -> None:
    running = await start_tcp(lambda req: {"status": "ok", "echo": req["action"]})
    assert running.host is not None and running.port is not None
    client = _client(Endpoint(host=running.host, port=running.port))
    response = await client.call({"action": "ping"}, timeout=5)
    assert response == {"status": "ok", "echo": "ping"}
    await client.aclose()


async def test_unix_round_trip(start_unix: Starter) -> None:
    running = await start_unix(lambda req: {"status": "ok", "echo": req["action"]})
    assert running.unix_path is not None
    client = _client(Endpoint(unix_path=running.unix_path))
    response = await client.call({"action": "status"}, timeout=5)
    assert response["echo"] == "status"
    await client.aclose()


async def test_connection_is_reused_across_calls(start_tcp: Starter) -> None:
    running = await start_tcp(lambda req: {"status": "ok"})
    assert running.host is not None and running.port is not None
    client = _client(Endpoint(host=running.host, port=running.port))
    for _ in range(3):
        await client.call({"action": "ping"}, timeout=5)
    assert running.daemon.connections == 1
    assert len(running.daemon.requests) == 3
    await client.aclose()


async def test_closed_without_reply_is_connection_error_and_next_call_reconnects(
    start_tcp: Starter,
) -> None:
    mode = {"drop": True}

    def handler(req: dict[str, object]) -> dict[str, object] | None:
        return None if mode["drop"] else {"status": "ok"}

    running = await start_tcp(handler)
    assert running.host is not None and running.port is not None
    client = _client(Endpoint(host=running.host, port=running.port))
    with pytest.raises(VoiceConnectionError):
        await client.call({"action": "ping"}, timeout=5)
    mode["drop"] = False
    assert await client.call({"action": "ping"}, timeout=5) == {"status": "ok"}
    assert running.daemon.connections == 2
    await client.aclose()


async def test_malformed_body_is_protocol_error_but_stream_stays_in_sync(
    start_tcp: Starter,
) -> None:
    replies: list[bytes | dict[str, object]] = [b"not json", {"status": "ok"}]

    def handler(req: dict[str, object]) -> bytes | dict[str, object]:
        return replies.pop(0)

    running = await start_tcp(handler)
    assert running.host is not None and running.port is not None
    client = _client(Endpoint(host=running.host, port=running.port))
    with pytest.raises(VoiceProtocolError):
        await client.call({"action": "ping"}, timeout=5)
    # The whole frame was consumed, so the stream is still in sync and the connection is reused.
    assert await client.call({"action": "ping"}, timeout=5) == {"status": "ok"}
    assert running.daemon.connections == 1
    await client.aclose()


async def test_slow_daemon_hits_deadline(start_tcp: Starter) -> None:
    running = await start_tcp(lambda req: {"status": "ok"}, delay=0.5)
    assert running.host is not None and running.port is not None
    client = _client(Endpoint(host=running.host, port=running.port))
    with pytest.raises(VoiceTimeoutError):
        await client.call({"action": "ping"}, timeout=0.05)
    await client.aclose()


async def test_connect_refused_is_connection_error() -> None:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    client = _client(Endpoint(host="127.0.0.1", port=port))
    with pytest.raises(VoiceConnectionError):
        await client.call({"action": "ping"}, timeout=5)


async def test_oversized_request_rejected_before_connecting(start_tcp: Starter) -> None:
    running = await start_tcp(lambda req: {"status": "ok"})
    assert running.host is not None and running.port is not None
    client = _client(Endpoint(host=running.host, port=running.port))
    with pytest.raises(VoiceInputError):
        await client.call({"blob": "x" * (51 * 1024 * 1024)}, timeout=5)
    assert running.daemon.connections == 0
    await client.aclose()


async def test_pool_bound_serialises_requests_on_one_connection(start_tcp: Starter) -> None:
    running = await start_tcp(lambda req: {"status": "ok", "n": req["n"]}, delay=0.02)
    assert running.host is not None and running.port is not None
    client = _client(Endpoint(host=running.host, port=running.port), max_connections=1)
    results = await asyncio.gather(
        *(client.call({"action": "ping", "n": i}, timeout=5) for i in range(4))
    )
    assert len(results) == 4
    assert {r["n"] for r in results} == {0, 1, 2, 3}
    assert running.daemon.connections == 1
    await client.aclose()


async def test_closed_client_refuses_calls(start_tcp: Starter) -> None:
    running = await start_tcp(lambda req: {"status": "ok"})
    assert running.host is not None and running.port is not None
    client = _client(Endpoint(host=running.host, port=running.port))
    await client.call({"action": "ping"}, timeout=5)
    await client.aclose()
    with pytest.raises(VoiceConnectionError):
        await client.call({"action": "ping"}, timeout=5)


async def test_call_timeout_must_be_positive() -> None:
    client = _client(Endpoint(host="127.0.0.1", port=1))
    with pytest.raises(ValueError):
        await client.call({"action": "ping"}, timeout=0)
