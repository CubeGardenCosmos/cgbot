"""Offline tests for ``sandbox/bridge/cgbot_report_mcp.py``.

``findata`` and ``richdoc`` are replaced by small shell fakes placed first on PATH. Their
behaviour is selected per test through environment variables, so no network or renderer is needed.
"""

from __future__ import annotations

import concurrent.futures
import importlib.util
import json
import math
import os
import struct
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "sandbox" / "bridge" / "cgbot_report_mcp.py"

FINDATA_FAKE = r"""#!/bin/sh
printf '%s\n' "$*" >> "$FAKE_LOG"
out=""
prev=""
for arg in "$@"; do
  if [ "$prev" = "--out" ]; then out="$arg"; fi
  prev="$arg"
done
case "$FAKE_FINDATA_MODE" in
  ok)
    if [ -n "$out" ]; then
      printf '[{"date":"2026-01-02","open":1,"high":2,"low":1,"close":2,"volume":3}]' > "$out"
    fi
    printf '{"source":"fake","data":[]}\n' ;;
  fail) printf '{"error":{"code":"upstream","message":"boom"}}\n'; exit 1 ;;
  stderr) echo "plain failure" >&2; exit 2 ;;
  bad) printf 'not json\n' ;;
  big) printf '{"blob":"'; head -c 13000 /dev/zero | tr '\0' 'a'; printf '"}\n' ;;
  sleep) sleep 30 ;;
esac
"""

RICHDOC_FAKE = r"""#!/bin/sh
printf '%s\n' "$*" >> "$FAKE_LOG"
out="$2"
case "$FAKE_RICHDOC_MODE" in
  ok)
    cp "$FAKE_PNG" "$out"
    printf '{"files":["%s"],"page_count":1,"errors":[]}\n' "$out" ;;
  errors)
    cp "$FAKE_PNG" "$out"
    printf '{"files":["%s"],"page_count":1,"errors":["KLine: bad data"]}\n' "$out" ;;
  fail) echo "richdoc: layout failed" >&2; exit 1 ;;
  sleep) sleep 30 ;;
esac
"""

BARS = [{"date": "2026-01-02", "open": 1, "high": 2, "low": 1, "close": 2, "volume": 3}]


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("cgbot_report_mcp", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


report = _load_script()


def _png_bytes(width: int, height: int) -> bytes:
    # Signature, IHDR length and type, then width/height; the rest is not read by the server.
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x06\x00\x00\x00\x00\x00\x00\x00"
    )


@dataclass(frozen=True)
class Fake:
    workspace: Path
    artifacts: Path
    log: Path


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Fake:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("findata", FINDATA_FAKE), ("richdoc", RICHDOC_FAKE)):
        path = bin_dir / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
    png = tmp_path / "fake.png"
    png.write_bytes(_png_bytes(1080, 640))
    log = tmp_path / "calls.log"
    log.write_text("", encoding="utf-8")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    artifacts = workspace / "artifacts"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("CGBOT_REPORT_ARTIFACTS_DIR", str(artifacts))
    monkeypatch.setenv("CGBOT_REPORT_ALLOWED_ROOTS", str(workspace))
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("FAKE_PNG", str(png))
    monkeypatch.setenv("FAKE_FINDATA_MODE", "ok")
    monkeypatch.setenv("FAKE_RICHDOC_MODE", "ok")
    return Fake(workspace=workspace, artifacts=artifacts, log=log)


def call(name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
    return report.run_tool(report.TOOLS[name], arguments)


def calls_log(fake: Fake) -> list[str]:
    return [line for line in fake.log.read_text(encoding="utf-8").splitlines() if line]


def assert_error(result: tuple[str, bool], code: str) -> str:
    text, is_error = result
    assert is_error is True, text
    assert text.startswith(f"({code})"), text
    return text


# ----------------------------------------------------------------- market_data


def test_market_data_kline_writes_bars_file_and_reports_path(fake: Fake) -> None:
    text, is_error = call("market_data", {"command": "kline", "args": ["600519", "--market", "CN"]})

    assert is_error is False, text
    first = text.splitlines()[0]
    assert first.startswith("bars file (pass as kline_chart file_path): ")
    bars_path = Path(first.split(": ", 1)[1])
    assert bars_path.parent == fake.artifacts.resolve()
    assert bars_path.name.startswith("kline-") and bars_path.suffix == ".json"
    assert json.loads(bars_path.read_text(encoding="utf-8")) == BARS
    assert calls_log(fake)[0].startswith("kline 600519 --market CN --out ")


def test_market_data_out_path_is_honoured_for_kline(fake: Fake) -> None:
    target = fake.workspace / "bars" / "600519.json"
    text, is_error = call(
        "market_data",
        {"command": "kline", "args": ["600519"], "out_path": str(target)},
    )

    assert is_error is False, text
    assert json.loads(target.read_text(encoding="utf-8")) == BARS


def test_market_data_quote_returns_printed_json(fake: Fake) -> None:
    text, is_error = call("market_data", {"command": "quote", "args": ["600519"]})

    assert is_error is False
    assert json.loads(text) == {"source": "fake", "data": []}


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"command": "shell"},
        {"command": "quote", "args": "600519"},
        {"command": "quote", "args": ["--out", "x.json"]},
        {"command": "kline", "args": ["--out=x.json"]},
        {"command": "quote", "args": ["a\nb"]},
        {"command": "quote", "args": ["x"] * 65},
        {"command": "quote", "out_path": "/workspace/x.json"},
        {"command": "kline", "out_path": "relative.json"},
        {"command": "kline", "out_path": "/tmp/not-allowed/x.json"},
    ],
    ids=[
        "missing-command",
        "unknown-command",
        "args-not-list",
        "out-flag",
        "out-equals",
        "newline-arg",
        "too-many-args",
        "out-path-non-kline",
        "out-path-relative",
        "out-path-outside-roots",
    ],
)
def test_market_data_rejects_invalid_arguments(fake: Fake, arguments: dict[str, Any]) -> None:
    text, is_error = call("market_data", arguments)

    assert is_error is True
    assert text.startswith("(invalid_argument)") or text.startswith("(path_not_allowed)"), text
    assert calls_log(fake) == []


def test_market_data_non_json_out_path_is_rejected(fake: Fake) -> None:
    target = fake.workspace / "bars.txt"
    assert_error(
        call("market_data", {"command": "kline", "args": ["x"], "out_path": str(target)}),
        "path_not_allowed",
    )


def test_market_data_nonzero_exit_surfaces_findata_error_json(
    fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_FINDATA_MODE", "fail")
    text = assert_error(call("market_data", {"command": "quote", "args": ["x"]}), "findata_failed")
    assert "boom" in text


def test_market_data_nonzero_exit_without_json_uses_stderr(
    fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_FINDATA_MODE", "stderr")
    text = assert_error(call("market_data", {"command": "quote", "args": ["x"]}), "findata_failed")
    assert "plain failure" in text


def test_market_data_rejects_non_json_stdout(fake: Fake, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_FINDATA_MODE", "bad")
    assert_error(call("market_data", {"command": "quote", "args": ["x"]}), "bad_output")


def test_market_data_timeout_kills_findata(fake: Fake, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(report, "FINDATA_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setenv("FAKE_FINDATA_MODE", "sleep")
    started = time.monotonic()
    assert_error(call("market_data", {"command": "quote", "args": ["x"]}), "timeout")
    assert time.monotonic() - started < 10


def test_market_data_large_output_is_clipped_and_saved(
    fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_FINDATA_MODE", "big")
    text, is_error = call("market_data", {"command": "quote", "args": ["x"]})

    assert is_error is False
    assert text.startswith("full output saved to ")
    assert text.endswith("...[clipped]")
    saved = Path(text.splitlines()[0].removeprefix("full output saved to "))
    assert saved.parent == fake.artifacts.resolve()
    assert saved.stat().st_size > report.INLINE_TEXT_CHARS


def test_missing_findata_binary_is_reported(
    fake: Fake, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    assert_error(call("market_data", {"command": "quote", "args": ["x"]}), "not_installed")


# ----------------------------------------------------------------- kline_chart


def test_kline_chart_from_data_renders_png_with_intl_colors(fake: Fake) -> None:
    text, is_error = call("kline_chart", {"data": BARS, "name": "chart_1", "title": "甲"})

    assert is_error is False, text
    assert "rendered 1 file(s), pages=1" in text
    assert f"{fake.artifacts.resolve() / 'chart_1.png'} (1080x640px)" in text
    assert "report_progress" in text
    markup = (fake.artifacts / "chart_1.md").read_text(encoding="utf-8")
    assert markup.startswith("<KLine ")
    assert 'colors={"intl"}' in markup
    assert 'title={"甲"}' in markup
    assert calls_log(fake)[0].endswith("--format png")


def test_kline_chart_cn_colors_only_when_requested(fake: Fake) -> None:
    text, is_error = call("kline_chart", {"data": BARS, "name": "cn", "colors": "cn"})

    assert is_error is False, text
    assert 'colors={"cn"}' in (fake.artifacts / "cn.md").read_text(encoding="utf-8")


def test_kline_chart_reads_bars_file_within_roots(fake: Fake) -> None:
    bars_file = fake.workspace / "bars.json"
    bars_file.write_text(json.dumps(BARS), encoding="utf-8")

    text, is_error = call("kline_chart", {"file_path": str(bars_file), "name": "from_file"})

    assert is_error is False, text
    assert (fake.artifacts / "from_file.png").exists()


def test_market_data_bars_feed_kline_chart(fake: Fake) -> None:
    text, is_error = call("market_data", {"command": "kline", "args": ["600519"]})
    assert is_error is False, text
    bars_path = text.splitlines()[0].split(": ", 1)[1]

    text, is_error = call("kline_chart", {"file_path": bars_path, "name": "chained"})

    assert is_error is False, text
    assert (fake.artifacts / "chained.png").exists()


def test_kline_chart_markup_includes_ma_markers_and_default_forecast_label(fake: Fake) -> None:
    forecast = {"start": "2026-01-05", "end": "2026-01-06", "series": [2.1, 2.2]}
    text, is_error = call(
        "kline_chart",
        {
            "data": BARS,
            "name": "full",
            "ma": [5, 10],
            "markers": [{"price": 1.5, "type": "support", "label": "支撑"}],
            "forecast": forecast,
        },
    )

    assert is_error is False, text
    markup = (fake.artifacts / "full.md").read_text(encoding="utf-8")
    assert "ma={[5, 10]}" in markup
    assert '"type": "support"' in markup
    assert '"label": "预测(非实测)"' in markup
    assert '"series": [2.1, 2.2]' in markup


def test_kline_chart_notes_when_richdoc_clips_bars(fake: Fake) -> None:
    many = [
        {"date": f"2020-01-{i:02d}" if i < 30 else f"2020-02-{i - 29:02d}", "close": 1.0}
        for i in range(1, 502)
    ]
    text, is_error = call("kline_chart", {"data": many, "name": "long"})

    assert is_error is False, text
    assert f"richdoc draws the latest {report.RICHDOC_BAR_LIMIT} of 501 bars" in text


@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        ({"data": BARS, "file_path": "/x.json"}, "invalid_argument"),
        ({}, "invalid_argument"),
        ({"data": []}, "invalid_argument"),
        ({"data": [1]}, "invalid_argument"),
        ({"data": BARS, "ma": []}, "invalid_argument"),
        ({"data": BARS, "ma": [0]}, "invalid_argument"),
        ({"data": BARS, "ma": [True]}, "invalid_argument"),
        ({"data": BARS, "ma": [5] * 9}, "invalid_argument"),
        ({"data": BARS, "markers": [{"price": 1, "type": "buy"}]}, "invalid_argument"),
        ({"data": BARS, "markers": [{"price": True, "type": "support"}]}, "invalid_argument"),
        ({"data": BARS, "markers": [{"price": math.nan, "type": "support"}]}, "invalid_argument"),
        ({"data": BARS, "markers": "x"}, "invalid_argument"),
        ({"data": BARS, "forecast": "x"}, "invalid_argument"),
        (
            {"data": BARS, "forecast": {"start": "2026-01-05", "end": "2026-01-06"}},
            "invalid_argument",
        ),
        (
            {
                "data": BARS,
                "forecast": {"start": "2026-01-05", "end": "2026-01-06", "series": [1] * 251},
            },
            "invalid_argument",
        ),
        (
            {
                "data": BARS,
                "forecast": {"start": "2026-01-05", "end": "2026-01-06", "series": [math.nan]},
            },
            "invalid_argument",
        ),
        ({"data": BARS, "colors": "red"}, "invalid_argument"),
        ({"data": BARS, "title": 5}, "invalid_argument"),
        ({"data": BARS, "title": "t" * 513}, "invalid_argument"),
        ({"data": [{"date": "2026-01-02", "close": math.nan}]}, "invalid_argument"),
        ({"data": BARS, "name": "../evil"}, "invalid_argument"),
        ({"data": BARS, "name": ".hidden"}, "invalid_argument"),
        ({"file_path": "bars.json"}, "path_not_allowed"),
        ({"file_path": "/etc/passwd"}, "path_not_allowed"),
    ],
    ids=[
        "both-sources",
        "no-source",
        "empty-bars",
        "non-object-row",
        "ma-empty",
        "ma-zero",
        "ma-bool",
        "ma-too-many",
        "marker-type",
        "marker-bool-price",
        "marker-nan-price",
        "markers-not-list",
        "forecast-not-object",
        "forecast-no-series",
        "forecast-too-long",
        "forecast-nan",
        "colors-unknown",
        "title-not-string",
        "title-too-long",
        "nan-close",
        "name-traversal",
        "name-dotfile",
        "file-relative",
        "file-outside-roots",
    ],
)
def test_kline_chart_rejects_invalid_arguments(
    fake: Fake, arguments: dict[str, Any], code: str
) -> None:
    assert_error(call("kline_chart", arguments), code)
    assert calls_log(fake) == []


def test_kline_chart_missing_file_is_not_found(fake: Fake) -> None:
    assert_error(call("kline_chart", {"file_path": str(fake.workspace / "none.json")}), "not_found")


def test_kline_chart_richdoc_errors_return_is_error_with_files(
    fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_RICHDOC_MODE", "errors")
    text = assert_error(call("kline_chart", {"data": BARS, "name": "bad"}), "render_errors")
    assert "KLine: bad data" in text
    assert "bad.png" in text


def test_kline_chart_richdoc_failure_is_reported(
    fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_RICHDOC_MODE", "fail")
    text = assert_error(call("kline_chart", {"data": BARS, "name": "x"}), "render_failed")
    assert "layout failed" in text


def test_kline_chart_richdoc_timeout(fake: Fake, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(report, "RICHDOC_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setenv("FAKE_RICHDOC_MODE", "sleep")
    assert_error(call("kline_chart", {"data": BARS, "name": "slow"}), "timeout")


# ----------------------------------------------------------------- render_report


def test_render_report_markdown_png_default(fake: Fake) -> None:
    text, is_error = call("render_report", {"markdown": "# 标题\n\n正文\n", "name": "rep"})

    assert is_error is False, text
    assert str(fake.artifacts.resolve() / "rep.png") in text
    assert (fake.artifacts / "rep.md").read_text(encoding="utf-8") == "# 标题\n\n正文\n"
    assert calls_log(fake)[0].endswith("--format png")


def test_render_report_pdf_format(fake: Fake) -> None:
    text, is_error = call("render_report", {"markdown": "# x", "name": "rep", "format": "pdf"})

    assert is_error is False, text
    assert str(fake.artifacts.resolve() / "rep.pdf") in text
    assert calls_log(fake)[0].endswith("--format pdf")


def test_render_report_markdown_path_inside_roots(fake: Fake) -> None:
    source = fake.workspace / "notes.md"
    source.write_text("# notes\n", encoding="utf-8")

    text, is_error = call("render_report", {"markdown_path": str(source), "name": "from_path"})

    assert is_error is False, text
    assert str(source.resolve()) in calls_log(fake)[0]


def test_render_report_markdown_path_outside_roots_rejected(
    fake: Fake, tmp_path_factory: pytest.TempPathFactory
) -> None:
    outside = tmp_path_factory.mktemp("outside") / "secret.md"
    outside.write_text("# nope\n", encoding="utf-8")
    assert_error(call("render_report", {"markdown_path": str(outside)}), "path_not_allowed")


def test_render_report_symlink_escape_rejected(
    fake: Fake, tmp_path_factory: pytest.TempPathFactory
) -> None:
    outside = tmp_path_factory.mktemp("outside-link") / "target.md"
    outside.write_text("# nope\n", encoding="utf-8")
    link = fake.workspace / "link.md"
    link.symlink_to(outside)
    assert_error(call("render_report", {"markdown_path": str(link)}), "path_not_allowed")


def test_render_report_relative_path_and_missing_file(fake: Fake) -> None:
    assert_error(call("render_report", {"markdown_path": "notes.md"}), "path_not_allowed")
    assert_error(
        call("render_report", {"markdown_path": str(fake.workspace / "missing.md")}),
        "not_found",
    )


@pytest.mark.parametrize(
    "arguments",
    [
        {"markdown": "# x", "markdown_path": "/workspace/x.md"},
        {},
        {"markdown": "   "},
        {"markdown": "# x", "format": "docx"},
        {"markdown": "# x", "name": "a/b"},
    ],
    ids=["both-sources", "no-source", "blank-markdown", "unknown-format", "name-with-slash"],
)
def test_render_report_rejects_invalid_arguments(fake: Fake, arguments: dict[str, Any]) -> None:
    assert_error(call("render_report", arguments), "invalid_argument")
    assert calls_log(fake) == []


def test_render_report_richdoc_failure_is_reported(
    fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_RICHDOC_MODE", "fail")
    text = assert_error(call("render_report", {"markdown": "# x", "name": "f"}), "render_failed")
    assert "layout failed" in text


# ----------------------------------------------------------------- tool table and errors


def test_tools_list_schemas_are_strict_objects() -> None:
    described = {tool["name"]: tool for tool in (t.describe() for t in report.TOOLS.values())}

    assert set(described) == {"market_data", "kline_chart", "render_report"}
    for tool in described.values():
        assert tool["description"]
        schema = tool["inputSchema"]
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
    market = described["market_data"]["inputSchema"]
    assert market["required"] == ["command"]
    assert market["properties"]["command"]["enum"] == list(report.FINDATA_COMMANDS)
    forecast = described["kline_chart"]["inputSchema"]["properties"]["forecast"]
    assert forecast["required"] == ["start", "end", "series"]
    for name in ("kline_chart", "render_report"):
        assert "report_progress" in described[name]["description"]


def test_run_tool_turns_unexpected_exceptions_into_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(arguments: dict[str, Any]) -> str:
        raise RuntimeError("secret detail")

    tool = report.Tool(name="boom", description="d", input_schema={}, handler=boom)
    monkeypatch.setattr(report, "log", lambda message: None)
    assert report.run_tool(tool, {}) == ("(internal) RuntimeError", True)


def test_artifacts_dir_default_and_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("CGBOT_REPORT_ARTIFACTS_DIR", raising=False)
    assert report.artifacts_dir() == os.path.realpath(report.DEFAULT_ARTIFACTS_DIR)
    monkeypatch.setenv("CGBOT_REPORT_ARTIFACTS_DIR", str(tmp_path / "out"))
    assert report.artifacts_dir() == os.path.realpath(tmp_path / "out")


# ----------------------------------------------------------------- JSON-RPC session


def _exchange(capsys: pytest.CaptureFixture[str], *messages: object) -> dict[Any, dict[str, Any]]:
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        session = report.Session(pool)
        for message in messages:
            raw = message if isinstance(message, bytes) else json.dumps(message).encode()
            session.handle_line(raw)
    lines = capsys.readouterr().out.splitlines()
    return {json.loads(line)["id"]: json.loads(line) for line in lines if line.strip()}


def test_initialize_negotiates_legacy_version(capsys: pytest.CaptureFixture[str]) -> None:
    out = _exchange(
        capsys,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        },
    )

    result = out[1]["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert result["serverInfo"] == report.SERVER_INFO


def test_initialize_unknown_version_falls_back_to_newest_legacy(
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = _exchange(
        capsys,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "1999-01-01"},
        },
    )
    assert out[1]["result"]["protocolVersion"] == report.LEGACY_VERSIONS[0]


def test_server_discover_and_modern_negotiation(capsys: pytest.CaptureFixture[str]) -> None:
    modern_meta = {report.META_PROTOCOL_KEY: report.PROTOCOL_MODERN}
    out = _exchange(
        capsys,
        {"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {"_meta": modern_meta}},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/list",
            "params": {"_meta": {report.META_PROTOCOL_KEY: "1999-01-01"}},
        },
    )

    assert out[1]["result"]["supportedVersions"] == [report.PROTOCOL_MODERN]
    assert out[1]["result"]["resultType"] == "complete"
    assert out[2]["result"]["resultType"] == "complete"
    assert {tool["name"] for tool in out[2]["result"]["tools"]} == set(report.TOOLS)
    assert out[3]["error"]["code"] == report.UNSUPPORTED_VERSION
    assert out[3]["error"]["data"] == {
        "requested": "1999-01-01",
        "supported": [report.PROTOCOL_MODERN],
    }


def test_ping_and_tools_list(capsys: pytest.CaptureFixture[str]) -> None:
    out = _exchange(
        capsys,
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )
    assert out[1]["result"] == {}
    assert [tool["name"] for tool in out[2]["result"]["tools"]] == list(report.TOOLS)


def test_protocol_errors(capsys: pytest.CaptureFixture[str]) -> None:
    out = _exchange(
        capsys,
        b"{bad json",
        {"jsonrpc": "2.0", "id": 4, "method": "no/such"},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "nope"}},
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "market_data", "arguments": "x"},
        },
        {"jsonrpc": "2.0", "id": 7, "method": "ping", "params": [1]},
        {"jsonrpc": "2.0", "id": 8, "method": 5},
    )

    batch_out = _exchange(capsys, b"[]")
    assert batch_out[None]["error"]["code"] == report.INVALID_REQUEST
    assert out[None]["error"]["code"] == report.PARSE_ERROR
    assert out[4]["error"]["code"] == report.METHOD_NOT_FOUND
    assert out[5]["error"]["code"] == report.INVALID_PARAMS
    assert out[6]["error"]["code"] == report.INVALID_PARAMS
    assert out[7]["error"]["code"] == report.INVALID_PARAMS
    assert out[8]["error"]["code"] == report.INVALID_REQUEST


def test_notifications_are_not_answered(capsys: pytest.CaptureFixture[str]) -> None:
    out = _exchange(capsys, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert out == {}


def test_tools_call_runs_through_session(fake: Fake, capsys: pytest.CaptureFixture[str]) -> None:
    out = _exchange(
        capsys,
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {"name": "kline_chart", "arguments": {"data": BARS, "name": "session"}},
        },
    )

    result = out[9]["result"]
    assert result["isError"] is False
    assert "rendered 1 file(s)" in result["content"][0]["text"]


def test_stdio_subprocess_handshake_list_and_call(fake: Fake) -> None:
    requests: list[dict[str, Any]] = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "kline_chart", "arguments": {"data": BARS, "name": "stdio"}},
        },
    ]
    payload = "".join(json.dumps(request) + "\n" for request in requests).encode()
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=payload,
        capture_output=True,
        timeout=60,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    responses = {
        message["id"]: message
        for message in (json.loads(line) for line in proc.stdout.decode().splitlines() if line)
    }
    assert set(responses) == {1, 2, 3}
    assert responses[1]["result"]["serverInfo"]["name"] == "cgbot-report"
    assert [tool["name"] for tool in responses[2]["result"]["tools"]] == list(report.TOOLS)
    assert responses[3]["result"]["isError"] is False
    assert (fake.artifacts / "stdio.png").exists()
