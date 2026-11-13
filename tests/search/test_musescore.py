"""MuseScore 工具层测试:假 exec runner 返回 cgdl 的 JSON,不触网、不碰 Docker。"""

from __future__ import annotations

import base64
import contextlib
import json
from collections.abc import AsyncGenerator, Sequence
from pathlib import Path
from typing import cast

import httpx
import pytest

from cgbot.agent.tools import ToolContext
from cgbot.coding.docker import DockerError, ExecResult, ForbiddenRoute
from cgbot.coding.sandbox import DockerSandboxProvider, SandboxError
from cgbot.model import Channel, OutboundMessage, SendResult
from cgbot.search.musescore import (
    BLOCKED_HINT,
    BLOCKED_TTL_S,
    E_BAD_OUTPUT,
    E_BAD_REF,
    E_BLOCKED,
    E_NO_PREVIEW,
    E_SANDBOX_UNAVAILABLE,
    EXEC_TIMEOUT_S,
    MAX_IMAGE_BYTES,
    MuseScoreClient,
    MuseScoreError,
    SandboxCgdlRunner,
    SandboxUnavailable,
    musescore_preview_tool,
    musescore_search_tool,
    parse_score_ref,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"fake-png-body"
PAGE_URL = "https://musescore.com/user/1001/scores/101"
OUT_PATH = "/workspace/artifacts/music/nocturne.png"
HIT = {
    "score_id": 101,
    "url": PAGE_URL,
    "title": "Nocturne Op. 9 No. 2",
    "composer": "Chopin",
    "instruments": ["Piano"],
    "pages": 4,
    "rating": 4.8,
    "rating_count": 12,
    "duration": "4:30",
    "license": "CC BY-SA",
}


def _ok(payload: object) -> ExecResult:
    body = json.dumps(payload, ensure_ascii=False).encode()
    return ExecResult(exit_code=0, stdout=body, stderr=b"")


def _err(code: str, message: str = "detail") -> ExecResult:
    body = json.dumps({"error": {"code": code, "message": message}}, ensure_ascii=False)
    return ExecResult(exit_code=1, stdout=b"", stderr=body.encode())


class FakeRunner:
    """按顺序返回预置结果;预置项为异常时抛出。无剩余结果却被调用即断言失败。"""

    def __init__(self, *responses: ExecResult | BaseException) -> None:
        self._responses: list[ExecResult | BaseException] = list(responses)
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    async def run(self, user_id: str, argv: Sequence[str]) -> ExecResult:
        self.calls.append((user_id, tuple(argv)))
        if not self._responses:
            raise AssertionError("unexpected cgdl exec")
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _Recorder:
    def __init__(self, *, ok: bool = True, error: str | None = None) -> None:
        self.sent: list[OutboundMessage] = []
        self._ok = ok
        self._error = error

    async def __call__(self, message: OutboundMessage) -> SendResult:
        self.sent.append(message)
        return SendResult(ok=self._ok, message_id="m1", error=self._error)


def _ctx(sender: _Recorder | None = None) -> ToolContext:
    return ToolContext(
        user_id="1001",
        session_id="private:1001",
        source_channel=Channel(kind="qq", account="1001"),
        source_message_id=None,
        sender=sender or _Recorder(),
    )


def _client(runner: FakeRunner, root: Path, clock: FakeClock | None = None) -> MuseScoreClient:
    return MuseScoreClient(runner, root, clock=clock or FakeClock())


def _write_png(root: Path, data: bytes = PNG) -> None:
    target = root / "users" / "1001" / "artifacts" / "music" / "nocturne.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


# ---------------------------------------------------------------------------
# 搜索工具


async def test_search_runs_cgdl_and_formats_hits(tmp_path: Path) -> None:
    runner = FakeRunner(_ok({"query": "nocturne piano", "results": [HIT]}))
    tool = musescore_search_tool(_client(runner, tmp_path))
    result = await tool.handler(_ctx(), {"query": "nocturne", "instrument": "piano", "limit": 3})
    assert result.is_error is False
    assert runner.calls == [
        ("1001", ("cgdl", "musescore-search", "--limit", "3", "--", "nocturne piano"))
    ]
    assert "Nocturne Op. 9 No. 2" in result.output
    assert f"乐谱 id 101 | {PAGE_URL}" in result.output
    assert "乐器「piano」已作为关键词加入搜索" in result.output


async def test_search_default_limit_and_empty_results(tmp_path: Path) -> None:
    runner = FakeRunner(_ok({"query": "zzz", "results": []}))
    result = await musescore_search_tool(_client(runner, tmp_path)).handler(
        _ctx(), {"query": "zzz"}
    )
    assert result.is_error is False
    assert runner.calls == [("1001", ("cgdl", "musescore-search", "--limit", "5", "--", "zzz"))]
    assert "没有找到" in result.output


async def test_search_rejects_bad_args_without_exec(tmp_path: Path) -> None:
    runner = FakeRunner()
    tool = musescore_search_tool(_client(runner, tmp_path))
    bad_args: list[dict[str, object]] = [
        {},
        {"query": "x", "limit": 0},
        {"query": "x", "limit": True},
        {"query": "x", "bogus": 1},
        {"query": 5},
    ]
    for args in bad_args:
        result = await tool.handler(_ctx(), args)  # type: ignore[arg-type]
        assert result.is_error is True
    assert runner.calls == []


@pytest.mark.parametrize(
    "code",
    [
        "not_found",
        "parse_failed",
        "download_failed",
        "unsupported_kind",
        "bad_url",
        "bad_input",
        "browser_unavailable",
    ],
)
async def test_each_cgdl_error_code_is_reported(tmp_path: Path, code: str) -> None:
    runner = FakeRunner(_err(code, "cgdl says no"))
    result = await musescore_search_tool(_client(runner, tmp_path)).handler(_ctx(), {"query": "x"})
    assert result.is_error is True
    assert f"({code}, retryable=true)" in result.output
    assert "cgdl says no" in result.output


@pytest.mark.parametrize(
    "payload",
    [
        {"query": "x", "results": "oops"},
        {"query": "x"},
        {"query": "x", "results": [{**HIT, "score_id": "101"}]},
        {"query": "x", "results": [{**HIT, "instruments": "Piano"}]},
        {"query": "x", "results": [{k: v for k, v in HIT.items() if k != "title"}]},
    ],
)
async def test_malformed_search_json_is_bad_output(tmp_path: Path, payload: object) -> None:
    runner = FakeRunner(_ok(payload))
    result = await musescore_search_tool(_client(runner, tmp_path)).handler(_ctx(), {"query": "x"})
    assert result.is_error is True
    assert f"({E_BAD_OUTPUT}, retryable=true)" in result.output


async def test_non_json_stdout_is_bad_output(tmp_path: Path) -> None:
    runner = FakeRunner(ExecResult(exit_code=0, stdout=b"<html>", stderr=b""))
    with pytest.raises(MuseScoreError) as info:
        await _client(runner, tmp_path).search("1001", "x", 3)
    assert info.value.code == E_BAD_OUTPUT


async def test_non_json_stderr_is_bad_output(tmp_path: Path) -> None:
    runner = FakeRunner(ExecResult(exit_code=1, stdout=b"", stderr=b"Traceback ..."))
    with pytest.raises(MuseScoreError) as info:
        await _client(runner, tmp_path).search("1001", "x", 3)
    assert info.value.code == E_BAD_OUTPUT


# ---------------------------------------------------------------------------
# blocked 语义与负缓存


async def test_blocked_is_not_retryable_and_negatively_cached(tmp_path: Path) -> None:
    clock = FakeClock()
    runner = FakeRunner(_err("blocked", "cloudflare challenge"))
    tool = musescore_search_tool(_client(runner, tmp_path, clock))

    first = await tool.handler(_ctx(), {"query": "x"})
    assert first.is_error is True
    assert BLOCKED_HINT in first.output
    assert "(blocked, retryable=false)" in first.output
    assert len(runner.calls) == 1

    # 负缓存期内再次调用立即返回,不再执行 cgdl(FakeRunner 无剩余结果,若执行即断言失败)。
    clock.now += BLOCKED_TTL_S - 1
    second = await tool.handler(_ctx(), {"query": "x"})
    assert second.is_error is True
    assert BLOCKED_HINT in second.output
    assert "retryable=false" in second.output
    assert len(runner.calls) == 1


async def test_blocked_raises_non_retryable_error(tmp_path: Path) -> None:
    runner = FakeRunner(_err("blocked"))
    with pytest.raises(MuseScoreError) as info:
        await _client(runner, tmp_path).search("1001", "x", 3)
    assert info.value.code == E_BLOCKED
    assert info.value.retryable is False
    assert info.value.message == BLOCKED_HINT


async def test_blocked_cache_expires_after_ttl(tmp_path: Path) -> None:
    clock = FakeClock()
    runner = FakeRunner(_err("blocked"), _ok({"query": "x", "results": [HIT]}))
    tool = musescore_search_tool(_client(runner, tmp_path, clock))
    await tool.handler(_ctx(), {"query": "x"})

    clock.now += BLOCKED_TTL_S
    result = await tool.handler(_ctx(), {"query": "x"})
    assert result.is_error is False
    assert len(runner.calls) == 2


async def test_blocked_cache_is_shared_with_preview(tmp_path: Path) -> None:
    runner = FakeRunner(_err("blocked"))
    client = _client(runner, tmp_path)
    await musescore_search_tool(client).handler(_ctx(), {"query": "x"})

    result = await musescore_preview_tool(client).handler(_ctx(), {"score": PAGE_URL})
    assert result.is_error is True
    assert BLOCKED_HINT in result.output
    assert len(runner.calls) == 1


# ---------------------------------------------------------------------------
# sandbox_unavailable 与沙盒 exec 适配层


async def test_sandbox_unavailable_is_reported(tmp_path: Path) -> None:
    runner = FakeRunner(SandboxUnavailable("no container"))
    result = await musescore_search_tool(_client(runner, tmp_path)).handler(_ctx(), {"query": "x"})
    assert result.is_error is True
    assert f"({E_SANDBOX_UNAVAILABLE}, retryable=true)" in result.output


class _Endpoint:
    def __init__(self, container: str) -> None:
        self.container = container


class _FakeDocker:
    def __init__(self, *, result: ExecResult | None = None, error: BaseException | None = None):
        self._result = result
        self._error = error
        self.calls: list[tuple[str, tuple[str, ...], object]] = []

    async def exec_run(self, container: str, cmd: Sequence[str], **options: object) -> ExecResult:
        self.calls.append((container, tuple(cmd), options.get("timeout_s")))
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


class _FakeSandbox:
    def __init__(self, docker: _FakeDocker, *, lease_error: BaseException | None = None) -> None:
        self.docker = docker
        self._lease_error = lease_error

    def lease(self, user_id: str) -> contextlib.AbstractAsyncContextManager[_Endpoint]:
        return self._enter(user_id)

    @contextlib.asynccontextmanager
    async def _enter(self, user_id: str) -> AsyncGenerator[_Endpoint, None]:
        if self._lease_error is not None:
            raise self._lease_error
        yield _Endpoint(f"cgdl-{user_id}")


def _runner(sandbox: _FakeSandbox) -> SandboxCgdlRunner:
    return SandboxCgdlRunner(cast(DockerSandboxProvider, sandbox))


async def test_sandbox_runner_execs_cgdl_in_user_container() -> None:
    docker = _FakeDocker(result=_ok({"query": "x", "results": []}))
    runner = _runner(_FakeSandbox(docker))
    result = await runner.run("1001", ["cgdl", "musescore-search", "x"])
    assert result.exit_code == 0
    assert docker.calls == [("cgdl-1001", ("cgdl", "musescore-search", "x"), EXEC_TIMEOUT_S)]


async def test_sandbox_runner_exit_127_is_unavailable() -> None:
    runner = _runner(_FakeSandbox(_FakeDocker(result=ExecResult(127, b"", b"not found"))))
    with pytest.raises(SandboxUnavailable):
        await runner.run("1001", ["cgdl", "musescore-search", "x"])


@pytest.mark.parametrize(
    "error",
    [
        DockerError(500, "boom"),
        ForbiddenRoute("boom"),
        httpx.ConnectError("boom"),
        OSError("boom"),
    ],
)
async def test_sandbox_exec_failures_are_unavailable(error: BaseException) -> None:
    runner = _runner(_FakeSandbox(_FakeDocker(error=error)))
    with pytest.raises(SandboxUnavailable):
        await runner.run("1001", ["cgdl", "musescore-search", "x"])


async def test_sandbox_lease_failure_is_unavailable() -> None:
    runner = _runner(_FakeSandbox(_FakeDocker(), lease_error=SandboxError("no sandbox")))
    with pytest.raises(SandboxUnavailable):
        await runner.run("1001", ["cgdl", "musescore-search", "x"])


async def test_unavailable_runner_surfaces_as_sandbox_unavailable_result(tmp_path: Path) -> None:
    runner = _runner(_FakeSandbox(_FakeDocker(error=DockerError(500, "proxy refused"))))
    result = await musescore_search_tool(MuseScoreClient(runner, tmp_path)).handler(
        _ctx(), {"query": "x"}
    )
    assert result.is_error is True
    assert f"({E_SANDBOX_UNAVAILABLE}, retryable=true)" in result.output


# ---------------------------------------------------------------------------
# 预览工具


async def test_preview_sends_png_from_workspace(tmp_path: Path) -> None:
    _write_png(tmp_path)
    runner = FakeRunner(_ok({"path": OUT_PATH, "title": "Nocturne"}))
    sender = _Recorder()
    result = await musescore_preview_tool(_client(runner, tmp_path)).handler(
        _ctx(sender), {"score": PAGE_URL}
    )
    assert result.is_error is False
    assert "《Nocturne》" in result.output
    assert runner.calls == [("1001", ("cgdl", "musescore-get", PAGE_URL, "--kind", "png"))]
    (message,) = sender.sent
    assert message.scope == "private"
    assert message.target_id == "1001"
    (segment,) = message.segments
    assert segment.type == "image"
    assert segment.data == {"file": "base64://" + base64.b64encode(PNG).decode("ascii")}


async def test_preview_bare_id_resolves_from_recent_search(tmp_path: Path) -> None:
    _write_png(tmp_path)
    runner = FakeRunner(
        _ok({"query": "x", "results": [HIT]}),
        _ok({"path": OUT_PATH, "title": None}),
    )
    client = _client(runner, tmp_path)
    await musescore_search_tool(client).handler(_ctx(), {"query": "x"})
    result = await musescore_preview_tool(client).handler(_ctx(_Recorder()), {"score": "101"})
    assert result.is_error is False
    assert runner.calls[1] == ("1001", ("cgdl", "musescore-get", PAGE_URL, "--kind", "png"))
    assert "id 101" in result.output


async def test_preview_unknown_id_is_bad_ref(tmp_path: Path) -> None:
    runner = FakeRunner()
    result = await musescore_preview_tool(_client(runner, tmp_path)).handler(
        _ctx(), {"score": "999"}
    )
    assert result.is_error is True
    assert f"({E_BAD_REF}, retryable=true)" in result.output
    assert runner.calls == []


async def test_preview_cgdl_error_is_reported(tmp_path: Path) -> None:
    runner = FakeRunner(_err("download_failed", "pdf fetch failed"))
    result = await musescore_preview_tool(_client(runner, tmp_path)).handler(
        _ctx(), {"score": PAGE_URL}
    )
    assert result.is_error is True
    assert "(download_failed, retryable=true)" in result.output


@pytest.mark.parametrize(
    ("path", "code"),
    [
        ("/workspace/artifacts/music/../../etc/x.png", E_BAD_OUTPUT),
        ("/workspace/other/nocturne.png", E_BAD_OUTPUT),
        ("/workspace/artifacts/music/nocturne.mxl", E_BAD_OUTPUT),
        ("/workspace/artifacts/music/missing.png", E_NO_PREVIEW),
    ],
)
async def test_preview_rejects_bad_output_paths(tmp_path: Path, path: str, code: str) -> None:
    _write_png(tmp_path)
    runner = FakeRunner(_ok({"path": path, "title": None}))
    result = await musescore_preview_tool(_client(runner, tmp_path)).handler(
        _ctx(_Recorder()), {"score": PAGE_URL}
    )
    assert result.is_error is True
    assert f"({code}, retryable=true)" in result.output


async def test_preview_rejects_non_png_content(tmp_path: Path) -> None:
    _write_png(tmp_path, data=b"GIF89a-not-png")
    runner = FakeRunner(_ok({"path": OUT_PATH, "title": None}))
    sender = _Recorder()
    result = await musescore_preview_tool(_client(runner, tmp_path)).handler(
        _ctx(sender), {"score": PAGE_URL}
    )
    assert result.is_error is True
    assert f"({E_NO_PREVIEW}, retryable=true)" in result.output
    assert sender.sent == []


async def test_preview_rejects_oversized_image(tmp_path: Path) -> None:
    _write_png(tmp_path, data=PNG + b"0" * (MAX_IMAGE_BYTES + 1))
    runner = FakeRunner(_ok({"path": OUT_PATH, "title": None}))
    result = await musescore_preview_tool(_client(runner, tmp_path)).handler(
        _ctx(_Recorder()), {"score": PAGE_URL}
    )
    assert result.is_error is True
    assert f"({E_NO_PREVIEW}, retryable=true)" in result.output


async def test_preview_send_failure_is_error(tmp_path: Path) -> None:
    _write_png(tmp_path)
    runner = FakeRunner(_ok({"path": OUT_PATH, "title": None}))
    sender = _Recorder(ok=False, error="napcat down")
    result = await musescore_preview_tool(_client(runner, tmp_path)).handler(
        _ctx(sender), {"score": PAGE_URL}
    )
    assert result.is_error is True
    assert "napcat down" in result.output


@pytest.mark.parametrize(
    "raw",
    [
        "http://musescore.com/user/1001/scores/101",
        "https://evil.example/user/1001/scores/101",
        "https://musescore.com/scores/101",
        "not a score",
    ],
)
def test_parse_score_ref_rejects_other_hosts_and_shapes(raw: str) -> None:
    with pytest.raises(MuseScoreError) as info:
        parse_score_ref(raw)
    assert info.value.code == E_BAD_REF


@pytest.mark.parametrize(
    ("raw", "score_id", "url"),
    [
        (PAGE_URL, 101, PAGE_URL),
        ("https://www.musescore.com/user/1001/scores/101/?x=1", 101, PAGE_URL),
        ("101", 101, None),
    ],
)
def test_parse_score_ref_accepts_page_url_and_bare_id(
    raw: str, score_id: int, url: str | None
) -> None:
    ref = parse_score_ref(raw)
    assert ref.score_id == score_id
    assert ref.url == url
