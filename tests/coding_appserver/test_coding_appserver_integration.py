"""Real-boundary smoke: a real ``codex app-server`` against a local fake model upstream.

Skipped unless ``CGBOT_CODEX_BIN`` points at a codex binary (and ``-m integration`` is given).
Codex refuses to create helper binaries under ``/tmp``, so the scratch CODEX_HOME is created
under ``~/.cache`` rather than pytest's tmp_path. Everything is removed afterwards. The bearer
token is written to a 0600 file and never printed.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import socket
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from aiohttp import ClientSession, web

from cgbot.coding.appserver import CodexAppServerClient, CodexConnectionError, connect_codex
from cgbot.coding.contracts import CodexEvent, SandboxEndpoint

pytestmark = pytest.mark.integration

CODEX_BIN = os.environ.get("CGBOT_CODEX_BIN", "")
BIN_OK = bool(CODEX_BIN) and Path(CODEX_BIN).is_file()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _sse(event_type: str, obj: dict) -> bytes:
    return f"event: {event_type}\ndata: {json.dumps(obj)}\n\n".encode()


async def _fake_responses(request: web.Request) -> web.StreamResponse:
    """Minimal Responses API: answers PONG, or hangs when the prompt contains SLOW."""
    body = await request.json()
    prompt = json.dumps(body.get("input", []))
    resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream"})
    await resp.prepare(request)
    rid = "resp_fake_1"
    await resp.write(
        _sse(
            "response.created",
            {"type": "response.created", "response": {"id": rid, "status": "in_progress"}},
        )
    )
    if "SLOW" in prompt:
        await asyncio.sleep(3600)
        return resp
    msg = {"type": "message", "id": "msg_fake_1", "role": "assistant", "status": "in_progress"}
    await resp.write(
        _sse(
            "response.output_item.added",
            {"type": "response.output_item.added", "output_index": 0, "item": msg},
        )
    )
    await resp.write(
        _sse(
            "response.output_text.delta",
            {
                "type": "response.output_text.delta",
                "item_id": "msg_fake_1",
                "output_index": 0,
                "content_index": 0,
                "delta": "PONG",
            },
        )
    )
    done = dict(
        msg,
        status="completed",
        content=[{"type": "output_text", "text": "PONG", "annotations": []}],
    )
    await resp.write(
        _sse(
            "response.output_item.done",
            {"type": "response.output_item.done", "output_index": 0, "item": done},
        )
    )
    usage = {
        "input_tokens": 10,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": 2,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": 12,
    }
    await resp.write(
        _sse(
            "response.completed",
            {
                "type": "response.completed",
                "response": {"id": rid, "status": "completed", "output": [done], "usage": usage},
            },
        )
    )
    await resp.write_eof()
    return resp


async def _events_until(
    client: CodexAppServerClient, kind: str, turn_id: str | None = None
) -> CodexEvent:
    async with asyncio.timeout(120):
        async for event in client.events():
            if event.kind == kind and (turn_id is None or event.turn_id == turn_id):
                return event
    raise AssertionError(f"event stream ended before {kind}")


@pytest.fixture
async def codex_server() -> AsyncIterator[SandboxEndpoint]:
    if not BIN_OK:
        pytest.skip("CGBOT_CODEX_BIN is unset or not a file")

    scratch = Path(tempfile.mkdtemp(prefix="cgbot-codex-it-", dir=Path.home() / ".cache"))
    upstream_port = _free_port()
    app_port = _free_port()
    token = secrets.token_urlsafe(24)
    token_file = scratch / "ws-token"
    token_file.write_text(token)
    token_file.chmod(0o600)
    home = scratch / "home"
    home.mkdir()
    (home / "config.toml").write_text(
        'model = "gpt-5.5"\n'
        'model_provider = "fake"\n'
        'approval_policy = "never"\n'
        'sandbox_mode = "danger-full-access"\n\n'
        "[model_providers.fake]\n"
        'name = "fake"\n'
        f'base_url = "http://127.0.0.1:{upstream_port}/v1"\n'
        'wire_api = "responses"\n'
    )

    upstream = web.Application()
    upstream.router.add_post("/v1/responses", _fake_responses)
    runner = web.AppRunner(upstream)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", upstream_port).start()

    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", str(Path.home())),
        "CODEX_HOME": str(home),
    }
    proc = await asyncio.create_subprocess_exec(
        CODEX_BIN,
        "app-server",
        "--listen",
        f"ws://127.0.0.1:{app_port}",
        "--ws-auth",
        "capability-token",
        "--ws-token-file",
        str(token_file),
        cwd=str(scratch),
        env=env,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await _wait_ready(app_port, proc)
        yield SandboxEndpoint(
            user_id="it-user",
            container="local",
            url=f"ws://127.0.0.1:{app_port}",
            ws_token=token,
        )
    finally:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), 10)
        except TimeoutError:
            proc.kill()
            await proc.wait()
        await runner.cleanup()
        shutil.rmtree(scratch, ignore_errors=True)


async def _wait_ready(port: int, proc: asyncio.subprocess.Process) -> None:
    async with asyncio.timeout(60):
        async with ClientSession() as session:
            while True:
                if proc.returncode is not None:
                    raise AssertionError(f"codex app-server exited early ({proc.returncode})")
                try:
                    async with session.get(f"http://127.0.0.1:{port}/readyz") as resp:
                        if resp.status == 200:
                            return
                except OSError:
                    pass
                await asyncio.sleep(0.2)


async def test_codex_app_server_round_trip(codex_server: SandboxEndpoint) -> None:
    client = await connect_codex(codex_server, request_timeout=60.0)
    try:
        thread = await client.thread_start(title="it-title")
        assert thread.title == "it-title"
        assert thread.archived is False
        assert thread.updated_at > 1_600_000_000  # unix seconds, not milliseconds

        turn_id = await client.turn_start(thread.thread_id, "say PONG")
        done = await _events_until(client, "turn_completed", turn_id)
        assert done.status == "completed"
        assert done.text == "PONG"

        listed = await client.thread_list()
        assert thread.thread_id in [t.thread_id for t in listed]

        turns, _cursor = await client.thread_read(thread.thread_id, limit=10)
        assert len(turns) == 1
        assert turns[0]["id"] == turn_id

        await client.thread_rename(thread.thread_id, "renamed")
        renamed = [t for t in await client.thread_list() if t.thread_id == thread.thread_id]
        assert renamed[0].title == "renamed"

        forked = await client.thread_fork(thread.thread_id)
        assert forked.thread_id != thread.thread_id

        await client.thread_archive(thread.thread_id)
        active_ids = [t.thread_id for t in await client.thread_list()]
        assert thread.thread_id not in active_ids
        everything = await client.thread_list(include_archived=True)
        assert (thread.thread_id, True) in [(t.thread_id, t.archived) for t in everything]

        await client.thread_delete(forked.thread_id)
        await client.thread_delete(thread.thread_id)
    finally:
        await client.close()


async def test_codex_app_server_steer_and_interrupt(codex_server: SandboxEndpoint) -> None:
    client = await connect_codex(codex_server, request_timeout=60.0)
    try:
        thread = await client.thread_start()
        turn_id = await client.turn_start(thread.thread_id, "SLOW please")
        await _events_until(client, "turn_started", turn_id)

        await client.turn_steer(thread.thread_id, turn_id, "also this")
        await client.turn_interrupt(thread.thread_id, turn_id)
        done = await _events_until(client, "turn_completed", turn_id)
        assert done.status == "interrupted"
    finally:
        await client.close()


async def test_codex_app_server_rejects_wrong_token(codex_server: SandboxEndpoint) -> None:
    bad = SandboxEndpoint(
        user_id=codex_server.user_id,
        container=codex_server.container,
        url=codex_server.url,
        ws_token="not-the-token",
    )
    with pytest.raises(CodexConnectionError) as info:
        await connect_codex(bad, open_timeout=10.0)
    assert "401" in str(info.value)
    assert "not-the-token" not in str(info.value)
    assert codex_server.ws_token not in str(info.value)
