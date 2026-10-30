"""R5.1: Responses gateway behaviour against a localhost fake upstream (offline)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import aiohttp
import pytest
from gateway_fakes import (
    TOKEN_SECRET,
    UPSTREAM_KEY,
    closed_port,
    running_web,
    sandbox_token,
    upstream,
)

from cgbot.coding.auth import derive_ws_token
from cgbot.config.settings import Secret

TIMEOUT = 5.0


def auth_headers(user_id: str = "10001") -> dict[str, str]:
    return {"Authorization": f"Bearer {sandbox_token(user_id)}"}


def url(port: int, path: str) -> str:
    return f"http://127.0.0.1:{port}{path}"


async def raw_status(port: int, request_line: str, extra: str = "") -> int:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(
            f"{request_line}\r\nHost: test\r\n{extra}Connection: close\r\n\r\n".encode("latin-1")
        )
        await writer.drain()
        status_line = await asyncio.wait_for(reader.readline(), TIMEOUT)
    finally:
        writer.close()
        await writer.wait_closed()
    return int(status_line.split()[1])


async def body_chunks(data: bytes) -> AsyncIterator[bytes]:
    for start in range(0, len(data), 4096):
        yield data[start : start + 4096]


async def test_missing_authorization_is_rejected_before_upstream(tmp_path: Path) -> None:
    statuses: list[int] = []
    async with (
        upstream() as up,
        running_web(tmp_path, upstream_base_url=up.base_url, statuses=statuses) as web,
    ):
        async with aiohttp.ClientSession(auto_decompress=False) as client:
            resp = await client.get(url(web.port, "/v1/echo"))
            assert resp.status == 401
            assert await resp.json() == {"error": "unauthorized"}
        assert up.requests == []
        assert statuses == []


@pytest.mark.parametrize(
    "token",
    [
        "cgb-forged.forged",
        "not-a-token",
        derive_ws_token(Secret(TOKEN_SECRET), "10001"),  # ws token is not a sandbox token
    ],
)
async def test_forged_or_wrong_kind_token_is_rejected(tmp_path: Path, token: str) -> None:
    statuses: list[int] = []
    async with (
        upstream() as up,
        running_web(tmp_path, upstream_base_url=up.base_url, statuses=statuses) as web,
    ):
        async with aiohttp.ClientSession(auto_decompress=False) as client:
            resp = await client.get(
                url(web.port, "/v1/echo"), headers={"Authorization": f"Bearer {token}"}
            )
            assert resp.status == 401
        assert up.requests == []


async def test_token_from_other_secret_is_rejected(tmp_path: Path) -> None:
    from cgbot.coding.auth import HmacSandboxAuth

    foreign = HmacSandboxAuth(Secret("some-other-deployment-secret")).token_for("10001")
    statuses: list[int] = []
    async with (
        upstream() as up,
        running_web(tmp_path, upstream_base_url=up.base_url, statuses=statuses) as web,
    ):
        async with aiohttp.ClientSession(auto_decompress=False) as client:
            resp = await client.get(
                url(web.port, "/v1/echo"), headers={"Authorization": f"Bearer {foreign}"}
            )
            assert resp.status == 401
        assert up.requests == []


async def test_request_is_forwarded_with_upstream_key_and_no_sandbox_token(
    tmp_path: Path,
) -> None:
    statuses: list[int] = []
    token = sandbox_token()
    payload = b'{"model":"m","input":"hi"}' * 7
    async with (
        upstream() as up,
        running_web(tmp_path, upstream_base_url=up.base_url, statuses=statuses) as web,
    ):
        async with aiohttp.ClientSession(auto_decompress=False) as client:
            resp = await client.post(
                url(web.port, "/v1/echo"),
                data=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Content-Encoding": "zstd",
                },
            )
            body = await resp.read()
            assert resp.status == 200
            assert body == payload
            assert resp.headers["Content-Encoding"] == "zstd"
            assert resp.headers["X-Upstream"] == "yes"

        assert len(up.requests) == 1
        seen = up.requests[0]
        assert seen.body == payload
        assert seen.header("Authorization") == f"Bearer {UPSTREAM_KEY}"
        assert seen.header("Content-Encoding") == "zstd"
        assert seen.header("Content-Type") == "application/json"
        for _, value in seen.headers:
            assert token not in value
            assert TOKEN_SECRET not in value
        assert statuses == [200]


async def test_upstream_path_and_query_are_preserved(tmp_path: Path) -> None:
    statuses: list[int] = []
    async with (
        upstream() as up,
        running_web(tmp_path, upstream_base_url=up.base_url, statuses=statuses) as web,
    ):
        async with aiohttp.ClientSession(auto_decompress=False) as client:
            resp = await client.get(
                url(web.port, "/v1/status/200?a=1&b=two%20words"), headers=auth_headers()
            )
            assert resp.status == 200
            assert await resp.json() == {"upstream_status": 200}
        assert up.requests[0].raw_path == "/v1/status/200?a=1&b=two%20words"


async def test_binary_body_passes_through_byte_identical(tmp_path: Path) -> None:
    statuses: list[int] = []
    expected = bytes(range(256)) * 512
    async with (
        upstream() as up,
        running_web(tmp_path, upstream_base_url=up.base_url, statuses=statuses) as web,
    ):
        async with aiohttp.ClientSession(auto_decompress=False) as client:
            resp = await client.get(url(web.port, "/v1/zstd-binary"), headers=auth_headers())
            body = await resp.read()
            assert resp.status == 200
            assert body == expected
            assert resp.headers["Content-Encoding"] == "zstd"
            assert int(resp.headers["Content-Length"]) == len(expected)


async def test_sse_events_are_streamed_incrementally(tmp_path: Path) -> None:
    statuses: list[int] = []
    async with (
        upstream() as up,
        running_web(tmp_path, upstream_base_url=up.base_url, statuses=statuses) as web,
    ):
        async with aiohttp.ClientSession(auto_decompress=False) as client:
            resp = await client.get(url(web.port, "/v1/sse"), headers=auth_headers())
            assert resp.status == 200
            # The upstream holds the second event until the gate opens: the first event must
            # reach the client before that, or the gateway is buffering.
            first = await asyncio.wait_for(resp.content.readline(), TIMEOUT)
            assert first == b"data: one\n"
            up.gate.set()
            rest = await asyncio.wait_for(resp.content.read(), TIMEOUT)
            assert rest == b"\ndata: two\n\n"


@pytest.mark.parametrize("code", [429, 500, 503])
async def test_upstream_error_status_passes_through_and_is_reported(
    tmp_path: Path, code: int
) -> None:
    statuses: list[int] = []
    async with (
        upstream() as up,
        running_web(tmp_path, upstream_base_url=up.base_url, statuses=statuses) as web,
    ):
        async with aiohttp.ClientSession(auto_decompress=False) as client:
            resp = await client.get(url(web.port, f"/v1/status/{code}"), headers=auth_headers())
            assert resp.status == code
            assert await resp.json() == {"upstream_status": code}
            if code == 429:
                assert resp.headers["Retry-After"] == "7"
        assert statuses == [code]


async def test_unreachable_upstream_returns_502_and_reports_zero(tmp_path: Path) -> None:
    statuses: list[int] = []
    base = f"http://127.0.0.1:{closed_port()}/v1"
    async with running_web(tmp_path, upstream_base_url=base, statuses=statuses) as web:
        async with aiohttp.ClientSession(auto_decompress=False) as client:
            resp = await client.post(url(web.port, "/v1/echo"), data=b"{}", headers=auth_headers())
            assert resp.status == 502
            assert await resp.json() == {"error": "upstream unavailable"}
        assert statuses == [0]


async def test_failing_status_hook_does_not_break_the_response(tmp_path: Path) -> None:
    from gateway_fakes import coding_config

    from cgbot.coding.web import CodingWeb

    def broken_hook(status: int) -> None:
        raise RuntimeError(f"hook exploded for {status}")

    async with upstream() as up:
        coding_web = CodingWeb(
            coding_config(tmp_path),
            upstream_base_url=up.base_url,
            upstream_api_key=Secret(UPSTREAM_KEY),
            status_hook=broken_hook,
        )
        await coding_web.start()
        try:
            async with aiohttp.ClientSession(auto_decompress=False) as client:
                resp = await client.get(
                    url(coding_web.port, "/v1/status/200"), headers=auth_headers()
                )
                assert resp.status == 200
                assert await resp.json() == {"upstream_status": 200}
        finally:
            await coding_web.stop()


async def test_mid_stream_upstream_failure_is_not_disguised_as_success(tmp_path: Path) -> None:
    statuses: list[int] = []
    async with (
        upstream() as up,
        running_web(tmp_path, upstream_base_url=up.base_url, statuses=statuses) as web,
    ):
        async with aiohttp.ClientSession(auto_decompress=False) as client:
            resp = await client.get(url(web.port, "/v1/break"), headers=auth_headers())
            assert resp.status == 200
            with pytest.raises(aiohttp.ClientError):
                await asyncio.wait_for(resp.read(), TIMEOUT)
        assert statuses == [200]


async def test_client_disconnect_while_waiting_cancels_upstream(tmp_path: Path) -> None:
    statuses: list[int] = []
    async with (
        upstream() as up,
        running_web(tmp_path, upstream_base_url=up.base_url, statuses=statuses) as web,
    ):
        async with aiohttp.ClientSession(auto_decompress=False) as client:
            task = asyncio.create_task(
                client.get(url(web.port, "/v1/hang"), headers=auth_headers())
            )
            await asyncio.wait_for(up.arrived.wait(), TIMEOUT)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        await asyncio.wait_for(up.cancelled.wait(), TIMEOUT)


async def test_client_disconnect_mid_stream_cancels_upstream(tmp_path: Path) -> None:
    statuses: list[int] = []
    async with (
        upstream() as up,
        running_web(tmp_path, upstream_base_url=up.base_url, statuses=statuses) as web,
    ):
        async with aiohttp.ClientSession(auto_decompress=False) as client:
            resp = await client.get(url(web.port, "/v1/stream-hang"), headers=auth_headers())
            first = await asyncio.wait_for(resp.content.readline(), TIMEOUT)
            assert first == b"data: first\n"
            resp.close()
        await asyncio.wait_for(up.cancelled.wait(), TIMEOUT)


async def test_hop_by_hop_headers_are_not_forwarded(tmp_path: Path) -> None:
    statuses: list[int] = []
    async with (
        upstream() as up,
        running_web(tmp_path, upstream_base_url=up.base_url, statuses=statuses) as web,
    ):
        async with aiohttp.ClientSession(auto_decompress=False) as client:
            resp = await client.get(
                url(web.port, "/v1/echo"),
                headers={
                    **auth_headers(),
                    "Connection": "X-Secret-Hop",
                    "X-Secret-Hop": "leak-me",
                    "Proxy-Authorization": "Basic c2VjcmV0",
                },
            )
            assert resp.status == 200
        seen = up.requests[0]
        assert seen.header("X-Secret-Hop") is None
        assert seen.header("Proxy-Authorization") is None


async def test_streaming_request_body_is_forwarded_intact(tmp_path: Path) -> None:
    statuses: list[int] = []
    payload = bytes(range(256)) * 300
    async with (
        upstream() as up,
        running_web(tmp_path, upstream_base_url=up.base_url, statuses=statuses) as web,
    ):
        async with aiohttp.ClientSession(auto_decompress=False) as client:
            resp = await client.post(
                url(web.port, "/v1/echo"),
                data=body_chunks(payload),
                headers=auth_headers(),
            )
            assert await resp.read() == payload
        assert up.requests[0].body == payload


@pytest.mark.parametrize(
    "request_line",
    [
        "GET /v1/../healthz HTTP/1.1",
        "GET /v1/%2e%2e/x HTTP/1.1",
        "GET /v1/a%5c..%5cx HTTP/1.1",
        "GET /v1/a%00b HTTP/1.1",
        "GET /v2/echo HTTP/1.1",
        "GET http://evil.example/v1/echo HTTP/1.1",
        "GET /v1/echo#frag HTTP/1.1",
    ],
)
async def test_unsafe_or_foreign_targets_never_reach_upstream(
    tmp_path: Path, request_line: str
) -> None:
    statuses: list[int] = []
    async with (
        upstream() as up,
        running_web(tmp_path, upstream_base_url=up.base_url, statuses=statuses) as web,
    ):
        status = await raw_status(
            web.port, request_line, extra=f"Authorization: Bearer {sandbox_token()}\r\n"
        )
        assert status in {400, 404}
        assert up.requests == []
        assert statuses == []


async def test_unsafe_target_without_auth_is_unauthorized(tmp_path: Path) -> None:
    statuses: list[int] = []
    async with (
        upstream() as up,
        running_web(tmp_path, upstream_base_url=up.base_url, statuses=statuses) as web,
    ):
        status = await raw_status(web.port, "GET /v1/../healthz HTTP/1.1")
        assert status == 401
        assert up.requests == []
