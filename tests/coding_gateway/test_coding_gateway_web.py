"""R5.1: CodingWeb assembly, health check, extra routes, and lifecycle (offline)."""

from __future__ import annotations

from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from gateway_fakes import UPSTREAM_KEY, coding_config, running_web, sandbox_token, upstream

from cgbot.coding.web import CodingWeb
from cgbot.config.settings import Secret


async def test_healthz_is_served_without_authorization(tmp_path: Path) -> None:
    statuses: list[int] = []
    async with running_web(
        tmp_path, upstream_base_url="http://127.0.0.1:9/v1", statuses=statuses
    ) as coding_web:
        async with aiohttp.ClientSession() as client:
            resp = await client.get(f"http://127.0.0.1:{coding_web.port}/healthz")
            assert resp.status == 200
            assert await resp.json() == {"status": "ok"}
    assert statuses == []


async def test_extra_routes_are_mounted_on_the_same_server(tmp_path: Path) -> None:
    async def bridge_ping(request: web.Request) -> web.Response:
        return web.json_response({"bridge": "pong"})

    statuses: list[int] = []
    async with running_web(
        tmp_path,
        upstream_base_url="http://127.0.0.1:9/v1",
        statuses=statuses,
        extra_routes=[web.get("/bridge/ping", bridge_ping)],
    ) as coding_web:
        async with aiohttp.ClientSession() as client:
            resp = await client.get(f"http://127.0.0.1:{coding_web.port}/bridge/ping")
            assert resp.status == 200
            assert await resp.json() == {"bridge": "pong"}


async def test_gateway_is_mounted_under_v1(tmp_path: Path) -> None:
    statuses: list[int] = []
    async with (
        upstream() as up,
        running_web(tmp_path, upstream_base_url=up.base_url, statuses=statuses) as coding_web,
    ):
        async with aiohttp.ClientSession() as client:
            resp = await client.get(
                f"http://127.0.0.1:{coding_web.port}/v1/status/200",
                headers={"Authorization": f"Bearer {sandbox_token()}"},
            )
            assert resp.status == 200
        assert up.requests[0].header("Authorization") == f"Bearer {UPSTREAM_KEY}"


async def test_port_is_unavailable_before_start(tmp_path: Path) -> None:
    coding_web = CodingWeb(
        coding_config(tmp_path),
        upstream_base_url="http://127.0.0.1:9/v1",
        upstream_api_key=Secret(UPSTREAM_KEY),
        status_hook=lambda status: None,
    )
    with pytest.raises(RuntimeError):
        _ = coding_web.port


async def test_start_twice_is_refused_and_stop_is_idempotent(tmp_path: Path) -> None:
    coding_web = CodingWeb(
        coding_config(tmp_path),
        upstream_base_url="http://127.0.0.1:9/v1",
        upstream_api_key=Secret(UPSTREAM_KEY),
        status_hook=lambda status: None,
    )
    await coding_web.start()
    try:
        assert coding_web.port > 0
        with pytest.raises(RuntimeError):
            await coding_web.start()
    finally:
        await coding_web.stop()
    await coding_web.stop()
