"""Tests for ``sandbox/bridge/cgbot_bridge_mcp.py``.

The MCP server runs as a real subprocess (stdio JSON-RPC) against a real aiohttp bridge built
from ``cgbot.coding.bridge.bridge_routes`` on 127.0.0.1 port 0. Files live under ``tmp_path``;
the server's allowed roots are pointed there via ``CGBOT_BRIDGE_ALLOWED_ROOTS``.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from aiohttp import web

from cgbot.coding.bridge import bridge_routes
from cgbot.coding.config import (
    BridgeConfig,
    CodexConfig,
    CodingConfig,
    ConcurrencyConfig,
    DockerConfig,
    HttpConfig,
    ImageGenConfig,
)
from cgbot.coding.contracts import CodingNotice
from cgbot.config.settings import Secret

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "sandbox" / "bridge" / "cgbot_bridge_mcp.py"
CHUNK = 2 * 1024 * 1024
MAX_BYTES = 512 * 1024 * 1024
MODERN = "2026-07-28"
META = "io.modelcontextprotocol/protocolVersion"
USER = "alice"
TIMEOUT = 20.0


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("cgbot_bridge_mcp", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _realpath(path: Path) -> str:
    return os.path.realpath(path)


bridge_mcp = _load_script()


class FakeAuth:
    """Dict-backed sandbox token store (``SandboxAuth`` shape)."""

    def __init__(self) -> None:
        self._users: dict[str, str] = {}

    def token_for(self, user_id: str) -> str:
        token = f"tok-{user_id}"
        self._users[token] = user_id
        return token

    def resolve(self, token: str) -> str | None:
        return self._users.get(token)


class FakeNotifier:
    def __init__(self) -> None:
        self.notices: list[CodingNotice] = []

    async def notify(self, notice: CodingNotice) -> None:
        self.notices.append(notice)


class ImagegenStub:
    """Stands in for the bot's ``/bridge/imagegen`` route (implemented by another slice)."""

    def __init__(self) -> None:
        self.status = 200
        self.content_type = "application/json"
        self.body = b""
        self.payloads: list[dict[str, Any]] = []

    async def handle(self, request: web.Request) -> web.Response:
        self.payloads.append(await request.json())
        return web.Response(status=self.status, body=self.body, content_type=self.content_type)


def _recorder(seen: list[tuple[str, str]]) -> Callable[..., Awaitable[web.StreamResponse]]:
    @web.middleware
    async def record(
        request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
    ) -> web.StreamResponse:
        seen.append((request.method, request.path))
        return await handler(request)

    return record


def make_config(tmp_path: Path) -> CodingConfig:
    return CodingConfig(
        token_secret=Secret("test-token-secret"),
        http=HttpConfig(host="127.0.0.1", port=0, public_base_url="http://cgbot.test:8000"),
        docker=DockerConfig(
            host="http://docker.test:2375",
            sandbox_image="cgbot-sandbox:test",
            network="test_net",
            host_data_dir=tmp_path / "data",
            egress_proxy="",
            idle_seconds=600.0,
            container_prefix="cgbot-sbx-",
        ),
        codex=CodexConfig(
            model="test-model",
            reasoning_effort="medium",
            appserver_port=8765,
            context_window=128000,
        ),
        concurrency=ConcurrencyConfig(
            per_user=4,
            degraded=1,
            degrade_failures=3,
            degrade_window_seconds=60.0,
            recover_after_seconds=3600.0,
        ),
        bridge=BridgeConfig(
            max_file_bytes=MAX_BYTES,
            chunk_bytes=CHUNK,
            temp_dir=tmp_path / "bridge-data",
            file_ttl_seconds=3600.0,
        ),
        imagegen=ImageGenConfig(
            base_url="https://images.test",
            api_key=Secret("imagegen-key"),
            model="image-test",
            mode="generations",
            timeout_seconds=30.0,
            proxy="",
            max_concurrency=1,
        ),
    )


@dataclass
class Harness:
    base_url: str
    token: str
    root: Path
    outside: Path
    notifier: FakeNotifier
    requests: list[tuple[str, str]]
    imagegen: ImagegenStub
    procs: list[McpProcess] = field(default_factory=list)


class McpProcess:
    """One running bridge script, spoken to over stdin/stdout."""

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self.proc = proc

    async def send_raw(self, data: bytes) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(data)
        await self.proc.stdin.drain()

    async def send(self, message: dict[str, Any]) -> None:
        await self.send_raw((json.dumps(message) + "\n").encode())

    async def read(self) -> dict[str, Any]:
        assert self.proc.stdout is not None
        line = await asyncio.wait_for(self.proc.stdout.readline(), TIMEOUT)
        assert line, "server closed stdout"
        message: dict[str, Any] = json.loads(line)
        return message

    async def call(
        self, method: str, params: dict[str, Any] | None = None, *, request_id: int = 1
    ) -> dict[str, Any]:
        await self.send(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
        )
        return await self.read()

    async def tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        meta: dict[str, Any] | None = None,
        request_id: int = 1,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"name": name, "arguments": arguments}
        if meta is not None:
            params["_meta"] = meta
        return await self.call("tools/call", params, request_id=request_id)

    async def close(self) -> tuple[bytes, str]:
        """Close stdin, wait for exit (must be 0); return leftover stdout and stderr."""
        assert self.proc.stdin is not None
        self.proc.stdin.close()
        stdout, stderr = await asyncio.wait_for(self.proc.communicate(), TIMEOUT)
        assert self.proc.returncode == 0, stderr.decode()
        return stdout, stderr.decode()


def server_env(h: Harness, **overrides: str | None) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "CGBOT_BRIDGE_URL": h.base_url,
        "CGBOT_BRIDGE_TOKEN": h.token,
        "CGBOT_USER_ID": USER,
        "CGBOT_BRIDGE_ALLOWED_ROOTS": str(h.root),
    }
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


async def spawn(h: Harness, **overrides: str | None) -> McpProcess:
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(SCRIPT),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=server_env(h, **overrides),
    )
    mcp = McpProcess(proc)
    h.procs.append(mcp)
    return mcp


@pytest.fixture
async def harness(tmp_path: Path) -> AsyncIterator[Harness]:
    root = tmp_path / "ws"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    notifier = FakeNotifier()
    auth = FakeAuth()
    requests: list[tuple[str, str]] = []
    imagegen = ImagegenStub()
    app = web.Application(middlewares=[_recorder(requests)])
    app.router.add_post("/bridge/imagegen", imagegen.handle)  # registered first: wins
    handle = bridge_routes(make_config(tmp_path), auth, notifier)
    await handle.start()
    app.add_routes(handle.routes)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    port = runner.addresses[0][1]
    h = Harness(
        base_url=f"http://127.0.0.1:{port}/bridge",
        token=auth.token_for(USER),
        root=root,
        outside=outside,
        notifier=notifier,
        requests=requests,
        imagegen=imagegen,
    )
    try:
        yield h
    finally:
        for mcp in h.procs:
            if mcp.proc.returncode is None:
                mcp.proc.kill()
                await mcp.proc.wait()
        await handle.close()
        await runner.cleanup()


def text_of(response: dict[str, Any]) -> str:
    text: str = response["result"]["content"][0]["text"]
    return text


def is_error(response: dict[str, Any]) -> bool:
    flag: bool = response["result"]["isError"]
    return flag


def uploads_created(h: Harness) -> int:
    return sum(1 for method, path in h.requests if method == "POST" and path == "/bridge/uploads")


def payload_bytes(size: int) -> bytes:
    return (bytes(range(256)) * (size // 256 + 1))[:size]


# ---------------------------------------------------------------- protocol


async def test_initialize_echoes_supported_legacy_version(harness: Harness) -> None:
    proc = await spawn(harness)
    response = await proc.call(
        "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}},
    )
    result = response["result"]
    assert response["id"] == 1
    assert result["protocolVersion"] == "2025-06-18"
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert result["serverInfo"] == {"name": "cgbot-bridge", "version": "1.0.0"}
    assert result["instructions"]
    assert "resultType" not in result
    await proc.close()


async def test_initialize_unknown_version_answers_latest(harness: Harness) -> None:
    proc = await spawn(harness)
    response = await proc.call("initialize", {"protocolVersion": "1999-01-01"})
    assert response["result"]["protocolVersion"] == "2025-11-25"
    await proc.close()


async def test_server_discover_describes_server(harness: Harness) -> None:
    proc = await spawn(harness)
    result = (await proc.call("server/discover"))["result"]
    assert result["resultType"] == "complete"
    assert result["supportedVersions"] == [MODERN]
    assert result["capabilities"] == {"tools": {}}
    assert result["_meta"] == {
        "io.modelcontextprotocol/serverInfo": {"name": "cgbot-bridge", "version": "1.0.0"}
    }
    assert result["ttlMs"] == 3600000
    assert result["cacheScope"] == "public"
    assert result["instructions"]
    await proc.close()


async def test_modern_request_with_wrong_version_is_rejected(harness: Harness) -> None:
    proc = await spawn(harness)
    response = await proc.call("tools/list", {"_meta": {META: "2025-11-25"}}, request_id=5)
    assert response["id"] == 5
    assert response["error"]["code"] == -32022
    assert response["error"]["message"] == "Unsupported protocol version"
    assert response["error"]["data"] == {"requested": "2025-11-25", "supported": [MODERN]}
    await proc.close()


async def test_modern_request_with_right_version_has_result_type(harness: Harness) -> None:
    proc = await spawn(harness)
    ping = await proc.call("ping", {"_meta": {META: MODERN}}, request_id=1)
    assert ping["result"] == {"resultType": "complete"}
    listed = await proc.call("tools/list", {"_meta": {META: MODERN}}, request_id=2)
    assert listed["result"]["resultType"] == "complete"
    assert len(listed["result"]["tools"]) == 3
    await proc.close()


async def test_legacy_ping_has_no_result_type(harness: Harness) -> None:
    proc = await spawn(harness)
    assert (await proc.call("ping"))["result"] == {}
    await proc.close()


async def test_notifications_get_no_response(harness: Harness) -> None:
    proc = await spawn(harness)
    await proc.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    await proc.send(
        {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 3}}
    )
    await proc.send({"jsonrpc": "2.0", "method": "custom/unknown", "params": {}})
    response = await proc.call("ping", request_id=9)
    assert response["id"] == 9
    stdout, _ = await proc.close()
    assert stdout == b""


async def test_tools_list_names_and_schemas(harness: Harness) -> None:
    proc = await spawn(harness)
    tools = {tool["name"]: tool for tool in (await proc.call("tools/list"))["result"]["tools"]}
    assert set(tools) == {"report_progress", "send_file", "generate_image"}
    for tool in tools.values():
        assert tool["description"]
        assert tool["inputSchema"]["type"] == "object"
    report = tools["report_progress"]["inputSchema"]
    assert report["required"] == ["message"]
    assert report["properties"]["status"]["enum"] == [
        "running",
        "waiting_user_input",
        "completed",
        "failed",
    ]
    assert report["properties"]["status"]["default"] == "running"
    assert report["properties"]["files"]["maxItems"] == 16
    assert tools["send_file"]["inputSchema"]["required"] == ["path"]
    image = tools["generate_image"]["inputSchema"]
    assert image["required"] == ["prompt"]
    assert image["properties"]["reference_images"]["maxItems"] == 8
    await proc.close()


async def test_unknown_tool_is_invalid_params(harness: Harness) -> None:
    proc = await spawn(harness)
    response = await proc.tool("does_not_exist", {}, request_id=4)
    assert response["id"] == 4
    assert response["error"]["code"] == -32602
    await proc.close()


async def test_unknown_method_is_method_not_found(harness: Harness) -> None:
    proc = await spawn(harness)
    response = await proc.call("resources/list", request_id=6)
    assert response["id"] == 6
    assert response["error"]["code"] == -32601
    await proc.close()


async def test_parse_error_and_invalid_request_shapes(harness: Harness) -> None:
    proc = await spawn(harness)
    await proc.send_raw(b"{not json\n")
    parse = await proc.read()
    assert parse["id"] is None and parse["error"]["code"] == -32700
    await proc.send_raw(b"[]\n")
    batch = await proc.read()
    assert batch["id"] is None and batch["error"]["code"] == -32600
    await proc.send_raw(b"42\n")
    scalar = await proc.read()
    assert scalar["error"]["code"] == -32600
    await proc.send({"jsonrpc": "2.0", "id": 3})
    no_method = await proc.read()
    assert no_method["id"] == 3 and no_method["error"]["code"] == -32600
    await proc.close()


async def test_missing_required_env_exits_2() -> None:
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(SCRIPT),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={"PATH": os.environ.get("PATH", ""), "CGBOT_BRIDGE_URL": "http://127.0.0.1:9/b"},
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), TIMEOUT)
    assert proc.returncode == 2
    assert b"CGBOT_BRIDGE_TOKEN" in stderr
    assert stdout == b""


# ---------------------------------------------------------------- report_progress / send_file


async def test_report_progress_streams_multi_chunk_file(harness: Harness) -> None:
    size = CHUNK + 5
    data = payload_bytes(size)
    path = harness.root / "big.bin"
    path.write_bytes(data)
    proc = await spawn(harness)
    response = await proc.tool(
        "report_progress",
        {"message": "hello", "status": "running", "files": [str(path)]},
        meta={"threadId": "thr-1"},
    )
    assert not is_error(response), text_of(response)
    assert text_of(response) == "reported: status=running files=1"
    assert sum(1 for method, _ in harness.requests if method == "PUT") == 2
    assert uploads_created(harness) == 1
    (notice,) = harness.notifier.notices
    assert notice.kind == "report"
    assert notice.user_id == USER
    assert notice.status == "running"
    assert notice.thread_id == "thr-1"
    assert notice.text == "hello"
    (stored,) = notice.files
    assert stored.name == "big.bin"
    assert stored.size == size
    assert stored.path.read_bytes() == data
    stdout, stderr = await proc.close()
    assert harness.token not in stderr
    assert harness.token.encode() not in stdout


@pytest.mark.parametrize("key", ["threadId", "thread_id"])
async def test_thread_id_forwarded_from_meta(harness: Harness, key: str) -> None:
    proc = await spawn(harness)
    response = await proc.tool("report_progress", {"message": "tick"}, meta={key: "thr-forward"})
    assert not is_error(response), text_of(response)
    assert text_of(response) == "reported: status=running files=0"
    (notice,) = harness.notifier.notices
    assert notice.thread_id == "thr-forward"
    assert notice.status == "running"
    assert notice.files == ()
    await proc.close()


# Shape observed on codex-cli 0.166.0 stdio tools/call (docs/reference/codex-appserver-notes.md
# section (d)): top-level threadId/sessionId/windowId/callId/itemId/progressToken plus
# x-codex-turn-metadata. No io.modelcontextprotocol/protocolVersion key, i.e. the legacy path.
CODEX_META = {
    "threadId": "thr-codex",
    "sessionId": "thr-codex",
    "windowId": "thr-codex:0",
    "callId": "call-1",
    "itemId": "item-1",
    "progressToken": "tok-1",
    "x-codex-turn-metadata": {
        "thread_id": "thr-codex",
        "session_id": "thr-codex",
        "turn_id": "turn-1",
        "model": "test-model",
        "sandbox_mode": "danger-full-access",
        "codex_version": "0.166.0",
        "turn_started_at_unix_ms": 1700000000000,
    },
}


async def test_codex_meta_shape_forwards_thread_id(harness: Harness) -> None:
    proc = await spawn(harness)
    response = await proc.tool(
        "report_progress", {"message": "codex", "status": "running"}, meta=CODEX_META
    )
    assert not is_error(response), text_of(response)
    assert text_of(response) == "reported: status=running files=0"
    (notice,) = harness.notifier.notices
    assert notice.thread_id == "thr-codex"
    assert notice.user_id == USER
    await proc.close()


async def test_turn_metadata_thread_id_is_used_when_top_level_missing(harness: Harness) -> None:
    proc = await spawn(harness)
    meta = {"x-codex-turn-metadata": {"thread_id": "thr-turn", "turn_id": "turn-9"}}
    response = await proc.tool("report_progress", {"message": "turn"}, meta=meta)
    assert not is_error(response), text_of(response)
    (notice,) = harness.notifier.notices
    assert notice.thread_id == "thr-turn"
    await proc.close()


async def test_top_level_thread_id_wins_over_turn_metadata(harness: Harness) -> None:
    proc = await spawn(harness)
    meta = {"threadId": "thr-top", "x-codex-turn-metadata": {"thread_id": "thr-turn"}}
    response = await proc.tool("report_progress", {"message": "both"}, meta=meta)
    assert not is_error(response), text_of(response)
    (notice,) = harness.notifier.notices
    assert notice.thread_id == "thr-top"
    await proc.close()


async def test_overlong_thread_id_is_dropped_not_sent(harness: Harness) -> None:
    proc = await spawn(harness)
    response = await proc.tool("report_progress", {"message": "long"}, meta={"threadId": "t" * 257})
    assert not is_error(response), text_of(response)
    (notice,) = harness.notifier.notices
    assert notice.thread_id is None
    await proc.close()


async def test_codex_meta_without_protocol_key_uses_legacy_path(harness: Harness) -> None:
    proc = await spawn(harness)
    response = await proc.call("tools/list", {"_meta": {"threadId": "thr-codex"}}, request_id=7)
    assert "result" in response
    assert "resultType" not in response["result"]
    await proc.close()


async def test_report_without_meta_has_no_thread_id(harness: Harness) -> None:
    proc = await spawn(harness)
    response = await proc.tool("report_progress", {"message": "bare", "status": "completed"})
    assert not is_error(response), text_of(response)
    assert text_of(response) == "reported: status=completed files=0"
    (notice,) = harness.notifier.notices
    assert notice.thread_id is None
    assert notice.status == "completed"
    await proc.close()


async def test_send_file_uploads_and_reports_caption(harness: Harness) -> None:
    path = harness.root / "note.txt"
    path.write_bytes(b"hello\n")
    proc = await spawn(harness)
    response = await proc.tool("send_file", {"path": str(path), "caption": "here it is"})
    assert not is_error(response), text_of(response)
    (notice,) = harness.notifier.notices
    (stored,) = notice.files
    assert text_of(response) == f"sent: note.txt ({stored.kind}, 6 bytes)"
    assert notice.kind == "report"
    assert notice.status == "running"
    assert notice.text == "here it is"
    assert stored.path.read_bytes() == b"hello\n"
    await proc.close()


async def test_oversize_file_refused_before_any_network_call(harness: Harness) -> None:
    path = harness.root / "huge.bin"
    with path.open("wb") as handle:
        handle.truncate(MAX_BYTES + 1)
    proc = await spawn(harness)
    response = await proc.tool("report_progress", {"message": "big", "files": [str(path)]})
    assert is_error(response)
    assert text_of(response) == f"(too_large) file is {MAX_BYTES + 1} bytes; limit is {MAX_BYTES}"
    assert harness.requests == []
    assert harness.notifier.notices == []
    await proc.close()


async def test_path_outside_roots_is_forbidden(harness: Harness) -> None:
    outside_file = harness.outside / "secret.txt"
    outside_file.write_text("nope")
    sibling = harness.root.parent / (harness.root.name + "2") / "x.txt"
    sibling.parent.mkdir()
    sibling.write_text("nope")
    proc = await spawn(harness)
    for target in (outside_file, sibling):
        response = await proc.tool("send_file", {"path": str(target)})
        assert is_error(response)
        assert text_of(response).startswith("(forbidden_path) ")
    assert harness.requests == []
    await proc.close()


async def test_symlink_escaping_root_is_forbidden(harness: Harness) -> None:
    target = harness.outside / "secret.txt"
    target.write_text("nope")
    link = harness.root / "escape.txt"
    os.symlink(target, link)
    proc = await spawn(harness)
    response = await proc.tool("send_file", {"path": str(link)})
    assert is_error(response)
    assert text_of(response).startswith("(forbidden_path) ")
    assert harness.requests == []
    await proc.close()


async def test_missing_and_relative_paths_are_rejected(harness: Harness) -> None:
    proc = await spawn(harness)
    missing = await proc.tool("send_file", {"path": str(harness.root / "absent.txt")})
    assert text_of(missing).startswith("(not_found) ")
    relative = await proc.tool("send_file", {"path": "relative.txt"})
    assert text_of(relative).startswith("(invalid_argument) ")
    directory = await proc.tool("send_file", {"path": str(harness.root)})
    assert text_of(directory).startswith("(not_found) ")
    assert harness.requests == []
    await proc.close()


async def test_report_validation_errors_come_before_network(harness: Harness) -> None:
    valid = harness.root / "ok.txt"
    valid.write_text("ok")
    outside = harness.outside / "bad.txt"
    outside.write_text("bad")
    cases: list[tuple[dict[str, Any], str]] = [
        ({}, "(invalid_argument) message is required"),
        ({"message": "   "}, "(invalid_argument) message must not be empty"),
        ({"message": "x" * 8001}, "(invalid_argument) message exceeds 8000 characters"),
        ({"message": "ok", "status": "done"}, "(invalid_argument) status must be one of"),
        ({"message": "ok", "files": "ok.txt"}, "(invalid_argument) files must be an array"),
        (
            {"message": "ok", "files": [str(valid)] * 17},
            "(invalid_argument) files allows at most 16 items",
        ),
        ({"message": "ok", "files": [str(valid), str(outside)]}, "(forbidden_path) "),
    ]
    proc = await spawn(harness)
    for arguments, expected in cases:
        response = await proc.tool("report_progress", arguments)
        assert is_error(response), arguments
        assert text_of(response).startswith(expected), text_of(response)
    assert harness.requests == []
    assert harness.notifier.notices == []
    await proc.close()


# ---------------------------------------------------------------- generate_image


async def test_generate_image_passes_body_through_exactly(harness: Harness) -> None:
    body = '{"path":"/home/agent/out.png","width":2,"height":2,"bytes":10,"size":"2x2"}'
    harness.imagegen.body = body.encode()
    proc = await spawn(harness)
    response = await proc.tool(
        "generate_image", {"prompt": "a cat", "size": "1024x1024", "name": "cat"}
    )
    assert not is_error(response)
    assert text_of(response) == body
    assert harness.imagegen.payloads == [{"prompt": "a cat", "size": "1024x1024", "name": "cat"}]
    await proc.close()


async def test_generate_image_reference_images_are_checked_and_resolved(harness: Harness) -> None:
    ref = harness.root / "ref.png"
    ref.write_bytes(b"\x89PNG")
    harness.imagegen.body = b'{"path":"/x.png","width":1,"height":1,"bytes":4,"size":"1x1"}'
    proc = await spawn(harness)
    response = await proc.tool("generate_image", {"prompt": "edit", "reference_images": [str(ref)]})
    assert not is_error(response), text_of(response)
    assert harness.imagegen.payloads == [{"prompt": "edit", "reference_images": [_realpath(ref)]}]
    outside = harness.outside / "ref.png"
    outside.write_bytes(b"\x89PNG")
    refused = await proc.tool(
        "generate_image", {"prompt": "edit", "reference_images": [str(outside)]}
    )
    assert is_error(refused)
    assert text_of(refused).startswith("(forbidden_path) ")
    assert len(harness.imagegen.payloads) == 1
    await proc.close()


async def test_generate_image_json_error_is_mapped(harness: Harness) -> None:
    harness.imagegen.status = 422
    harness.imagegen.body = b'{"error":"policy","message":"blocked by policy"}'
    proc = await spawn(harness)
    response = await proc.tool("generate_image", {"prompt": "x"})
    assert is_error(response)
    assert text_of(response) == "(policy) blocked by policy"
    await proc.close()


async def test_generate_image_non_json_error_uses_status_and_snippet(harness: Harness) -> None:
    harness.imagegen.status = 500
    harness.imagegen.content_type = "text/plain"
    harness.imagegen.body = ("boom" + "x" * 300).encode()
    proc = await spawn(harness)
    response = await proc.tool("generate_image", {"prompt": "x"})
    assert is_error(response)
    assert text_of(response) == "(http_500) " + ("boom" + "x" * 300)[:200]
    assert len(harness.imagegen.payloads) == 1  # never retried
    await proc.close()


async def test_generate_image_unreachable_bridge(tmp_path: Path) -> None:
    # Nothing listens on this port: connection refused maps to (unreachable).
    root = tmp_path / "ws"
    root.mkdir()
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(SCRIPT),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={
            "PATH": os.environ.get("PATH", ""),
            "CGBOT_BRIDGE_URL": "http://127.0.0.1:1/bridge",
            "CGBOT_BRIDGE_TOKEN": "tok",
            "CGBOT_BRIDGE_ALLOWED_ROOTS": str(root),
        },
    )
    mcp = McpProcess(proc)
    try:
        response = await mcp.tool("generate_image", {"prompt": "x"})
        assert is_error(response)
        assert text_of(response).startswith("(unreachable) ")
        await mcp.close()
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


# ---------------------------------------------------------------- upload pipeline (stub server)


@dataclass
class ChunkStub:
    """Localhost stub for upload retry policy; statuses are consumed per PUT (0 = drop socket)."""

    chunk_statuses: list[int]
    create_status: int = 201
    creates: int = 0
    puts: int = 0
    completes: int = 0


@pytest.fixture
async def chunk_stub() -> AsyncIterator[tuple[ChunkStub, str]]:
    stub = ChunkStub(chunk_statuses=[200])

    async def create(request: web.Request) -> web.Response:
        stub.creates += 1
        await request.read()
        if stub.create_status != 201:
            return web.json_response(
                {"error": "boom", "message": "create failed"}, status=stub.create_status
            )
        return web.json_response({"upload_id": "up-1"}, status=201)

    async def put(request: web.Request) -> web.Response:
        await request.read()
        status = stub.chunk_statuses[min(stub.puts, len(stub.chunk_statuses) - 1)]
        stub.puts += 1
        if status == 0:
            assert request.transport is not None
            request.transport.close()
            return web.Response(status=500)
        if status >= 300:
            return web.json_response({"error": "x", "message": "y"}, status=status)
        return web.json_response({"index": 0, "received": 0})

    async def complete(request: web.Request) -> web.Response:
        stub.completes += 1
        await request.read()
        return web.json_response(
            {"upload_id": "up-1", "name": "a.txt", "mime": "text/plain", "size": 3, "kind": "file"}
        )

    app = web.Application()
    app.router.add_post("/bridge/uploads", create)
    app.router.add_put("/bridge/uploads/{upload_id}/chunks/{index}", put)
    app.router.add_post("/bridge/uploads/{upload_id}/complete", complete)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    port = runner.addresses[0][1]
    try:
        yield stub, f"http://127.0.0.1:{port}/bridge"
    finally:
        await runner.cleanup()


def _stub_upload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, base_url: str
) -> Callable[[], Awaitable[dict[str, Any]]]:
    root = tmp_path / "stub-ws"
    root.mkdir()
    (root / "a.txt").write_text("abc")
    monkeypatch.setenv("CGBOT_BRIDGE_ALLOWED_ROOTS", str(root))
    monkeypatch.setattr(bridge_mcp, "CHUNK_RETRY_DELAYS", (0.0, 0.0))
    target = bridge_mcp.check_file(str(root / "a.txt"))
    client = bridge_mcp.BridgeClient(base_url, "tok")

    async def run() -> dict[str, Any]:
        result: dict[str, Any] = await asyncio.to_thread(bridge_mcp.upload_file, client, target)
        return result

    return run


@pytest.mark.parametrize(
    ("statuses", "expected_puts", "raises"),
    [
        ([500, 503, 200], 3, False),  # transient 5xx retried
        ([0, 200], 2, False),  # dropped connection retried
        ([500], 3, True),  # gives up after three attempts
        ([413], 1, True),  # 4xx never retried
    ],
)
async def test_chunk_put_retry_policy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    chunk_stub: tuple[ChunkStub, str],
    statuses: list[int],
    expected_puts: int,
    raises: bool,
) -> None:
    stub, base_url = chunk_stub
    stub.chunk_statuses = statuses
    run = _stub_upload(monkeypatch, tmp_path, base_url)
    if raises:
        with pytest.raises(bridge_mcp.BridgeHTTPError):
            await run()
    else:
        result = await run()
        assert result["upload_id"] == "up-1"
        assert stub.completes == 1
    assert stub.puts == expected_puts
    assert stub.creates == 1


async def test_create_is_never_retried(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, chunk_stub: tuple[ChunkStub, str]
) -> None:
    stub, base_url = chunk_stub
    stub.create_status = 500
    run = _stub_upload(monkeypatch, tmp_path, base_url)
    with pytest.raises(bridge_mcp.BridgeHTTPError):
        await run()
    assert stub.creates == 1
    assert stub.puts == 0
