"""Offline search MCP schema, forwarding, failure and privacy coverage."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from aiohttp import web

SCRIPT = Path(__file__).resolve().parents[2] / "sandbox/bridge/cgbot_bridge_mcp.py"


@pytest.fixture(scope="module")
def mcp() -> ModuleType:
    spec = importlib.util.spec_from_file_location("searchbridge_mcp_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("name", "path", "arguments", "timeout"),
    [
        ("web_search", "/search", {"query": "public research"}, 120.0),
        (
            "web_search",
            "/search",
            {"query": "公开资料", "engine": "configured", "count": 3, "freshness": "week"},
            120.0,
        ),
        ("gemini_search", "/gemini-search", {"query": "public research"}, 180.0),
        (
            "gemini_search",
            "/gemini-search",
            {"query": "公开资料", "recency": "month"},
            180.0,
        ),
    ],
)
def test_search_forwards_authenticated_json_and_exact_text(
    mcp: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    path: str,
    arguments: dict[str, Any],
    timeout: float,
) -> None:
    seen: list[tuple[Any, ...]] = []
    answer = "来源\nhttps://example.test/source\n{not JSON}\n"

    class Connection:
        def __init__(self, host: str, port: int | None, *, timeout: float) -> None:
            seen.append(("connect", host, port, timeout))

        def request(self, method: str, path: str, *, body: bytes, headers: dict) -> None:
            seen.append((method, path, json.loads(body), headers))

        def getresponse(self) -> Any:
            return self

        status = 200

        def read(self) -> bytes:
            return answer.encode()

        def close(self) -> None:
            seen.append(("close",))

    monkeypatch.setattr(mcp.http.client, "HTTPConnection", Connection)
    client = mcp.BridgeClient("http://bridge.test:8000/bridge", "offline-test-token")
    assert mcp.run_tool(mcp.TOOLS[name], client, arguments, "thread-not-forwarded") == (
        answer,
        False,
    )
    assert seen == [
        ("connect", "bridge.test", 8000, timeout),
        (
            "POST",
            "/bridge" + path,
            arguments,
            {
                "Authorization": "Bearer offline-test-token",
                "Content-Type": "application/json",
            },
        ),
        ("close",),
    ]


@pytest.mark.parametrize("name,limit", [("web_search", 200), ("gemini_search", 300)])
def test_search_schemas(mcp: ModuleType, name: str, limit: int) -> None:
    tools = {tool["name"]: tool for tool in mcp._dispatch("tools/list", {})["tools"]}
    schema = tools[name]["inputSchema"]
    assert schema["required"] == ["query"]
    assert schema["properties"]["query"] == {"type": "string", "minLength": 1, "maxLength": limit}
    period = "freshness" if name == "web_search" else "recency"
    assert schema["properties"][period]["enum"] == ["day", "week", "month", "year"]
    if name == "web_search":
        assert schema["properties"]["count"]["minimum"] == 1
        assert "maximum" not in schema["properties"]["count"]


@pytest.mark.parametrize(
    "name,arguments",
    [
        (name, arguments)
        for name, limit in [("web_search", 200), ("gemini_search", 300)]
        for arguments in [{}, {"query": " "}, {"query": 3}, {"query": "x" * (limit + 1)}]
    ]
    + [
        ("web_search", {"query": "x", "count": count})
        for count in [True, False, 0, -1, 1.5, "3", None]
    ]
    + [
        ("web_search", {"query": "x", "engine": 3}),
        ("web_search", {"query": "x", "freshness": "hour"}),
        ("gemini_search", {"query": "x", "recency": "hour"}),
        ("gemini_search", {"query": "x", "recency": 3}),
    ],
)
def test_invalid_search_arguments_never_send(
    mcp: ModuleType, monkeypatch: pytest.MonkeyPatch, name: str, arguments: dict[str, Any]
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> str:
        pytest.fail("invalid arguments reached HTTP")

    monkeypatch.setattr(mcp.BridgeClient, "post_text", forbidden)
    client = mcp.BridgeClient("http://bridge.test/bridge", "offline-test-token")
    text, failed = mcp.run_tool(mcp.TOOLS[name], client, arguments, None)
    assert failed
    assert text.startswith("(invalid_argument) ")


@pytest.mark.parametrize("name", ["web_search", "gemini_search"])
@pytest.mark.parametrize(
    "error,expected",
    [
        (404, "(E_DISABLED) backend unavailable"),
        (403, "(E_REFUSED) request refused"),
        (500, "(http_500) upstream failed"),
        (0, "(unreachable) TimeoutError"),
    ],
)
def test_search_errors_are_not_retried_or_logged_with_query(
    mcp: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
    error: int,
    expected: str,
) -> None:
    calls = 0
    query = "private-test-query-do-not-log"
    token = "private-test-token-do-not-log"

    def fail(*args: Any, **kwargs: Any) -> str:
        nonlocal calls
        calls += 1
        if error == 0:
            raise TimeoutError(query + token)
        if error == 500:
            raise mcp.BridgeHTTPError(error, b"upstream failed")
        code = "E_DISABLED" if error == 404 else "E_REFUSED"
        message = "backend unavailable" if error == 404 else "request refused"
        raise mcp.BridgeHTTPError(error, json.dumps({"error": code, "message": message}).encode())

    monkeypatch.setattr(mcp.BridgeClient, "post_text", fail)
    client = mcp.BridgeClient("http://bridge.test/bridge", token)
    assert mcp.run_tool(mcp.TOOLS[name], client, {"query": query}, None) == (expected, True)
    assert calls == 1
    captured = capsys.readouterr()
    assert query not in captured.out + captured.err
    assert token not in captured.out + captured.err
    assert captured.out == ""


@pytest.mark.parametrize(
    "name,path",
    [("web_search", "/bridge/search"), ("gemini_search", "/bridge/gemini-search")],
)
async def test_search_stdio_returns_bridge_text(name: str, path: str) -> None:
    seen: list[dict[str, Any]] = []
    answer = "Retrieved sources\nhttps://example.test/reference\n"

    async def handle(request: web.Request) -> web.Response:
        assert request.headers["Authorization"] == "Bearer offline-test-token"
        seen.append(await request.json())
        return web.Response(text=answer)

    app = web.Application()
    app.router.add_post(path, handle)
    runner = web.AppRunner(app)
    await runner.setup()
    proc: asyncio.subprocess.Process | None = None
    try:
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        port = runner.addresses[0][1]
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(SCRIPT),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={
                "CGBOT_BRIDGE_URL": f"http://127.0.0.1:{port}/bridge",
                "CGBOT_BRIDGE_TOKEN": "offline-test-token",
            },
        )
        assert proc.stdin is not None and proc.stdout is not None
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": {"query": "offline research"}},
        }
        proc.stdin.write((json.dumps(request) + "\n").encode())
        await proc.stdin.drain()
        response = json.loads(await asyncio.wait_for(proc.stdout.readline(), 20.0))
        assert response["id"] == 1
        assert response["result"]["isError"] is False
        assert response["result"]["content"] == [{"type": "text", "text": answer}]
        proc.stdin.close()
        stdout, stderr = await asyncio.wait_for(proc.communicate(), 20.0)
        assert proc.returncode == 0
        assert stdout == b""
        assert b"offline research" not in stderr
        assert b"offline-test-token" not in stderr
        assert seen == [{"query": "offline research"}]
    finally:
        if proc is not None and proc.returncode is None:
            proc.kill()
            await proc.wait()
        await runner.cleanup()
