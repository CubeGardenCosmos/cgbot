#!/usr/bin/env python3
"""cgbot report: in-sandbox MCP server (stdio, newline-delimited JSON-RPC) launched by Codex.

Tools: ``market_data`` (findata CLI), ``kline_chart`` (OHLCV K-line PNG) and ``render_report``
(richdoc Markdown to PNG/PDF). Each runs a binary found on PATH with a fixed argv; no shell.
Outputs land in the artifacts directory. Deliver them to the user with the bridge's
``report_progress`` ``files`` argument.

Environment:
    CGBOT_REPORT_ARTIFACTS_DIR  optional, default ``/workspace/artifacts``
    CGBOT_REPORT_ALLOWED_ROOTS  optional, os.pathsep-separated; default ``/workspace:/home/agent``

stdout carries JSON-RPC messages only; diagnostics go to stderr as names and counts, never
report bodies. Stdlib only.
"""

from __future__ import annotations

import concurrent.futures
import json
import math
import os
import re
import secrets
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import IO, Any

SERVER_INFO = {"name": "cgbot-report", "version": "1.0.0"}
INSTRUCTIONS = (
    "cgbot report: market_data runs findata for quotes, K-line bars, sectors and funds; "
    "kline_chart draws OHLCV K-line PNGs; render_report renders Markdown to PNG or PDF with "
    "richdoc. Deliver returned file paths with the bridge report_progress files argument."
)

PROTOCOL_MODERN = "2026-07-28"
LEGACY_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
META_PROTOCOL_KEY = "io.modelcontextprotocol/protocolVersion"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
UNSUPPORTED_VERSION = -32022

DEFAULT_ARTIFACTS_DIR = "/workspace/artifacts"
DEFAULT_ALLOWED_ROOTS = os.pathsep.join(("/workspace", "/home/agent"))
TOOL_WORKERS = 4
FINDATA_TIMEOUT_SECONDS = 60.0
RICHDOC_TIMEOUT_SECONDS = 180.0
PROCESS_OUTPUT_CAP_BYTES = 64 * 1024 * 1024
INPUT_FILE_CAP_BYTES = 32 * 1024 * 1024
INLINE_TEXT_CHARS = 12000
DIAGNOSTIC_CHARS = 2000
MAX_PATH_CHARS = 4096
MAX_MARKDOWN_CHARS = 2_000_000
MAX_BARS = 5000
RICHDOC_BAR_LIMIT = 500
MAX_ARGS = 64
MAX_ARG_CHARS = 512
MAX_MA_PERIODS = 8
MAX_MARKERS = 16
MAX_FORECAST_SERIES = 250
FORECAST_LABEL = "预测(非实测)"
FINDATA_COMMANDS = ("resolve", "quote", "kline", "overview", "sectors", "fund", "batch")
COLOR_MODES = ("intl", "cn")
MARKER_TYPES = ("support", "resistance")
RENDER_FORMATS = ("png", "pdf", "both")
NAME_PATTERN = re.compile(r"[\w-][\w.-]{0,99}")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def log(message: str) -> None:
    print(f"cgbot-report: {message}", file=sys.stderr, flush=True)


class RpcError(Exception):
    """JSON-RPC protocol error answered with the request id."""

    def __init__(self, code: int, message: str, data: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class ToolError(Exception):
    """A tool failed; rendered as ``(code) message`` with ``isError: true``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def render(self) -> str:
        return f"({self.code}) {self.message}"


def artifacts_dir() -> str:
    return os.path.realpath(os.environ.get("CGBOT_REPORT_ARTIFACTS_DIR") or DEFAULT_ARTIFACTS_DIR)


def _allowed_roots() -> list[str]:
    raw = os.environ.get("CGBOT_REPORT_ALLOWED_ROOTS") or DEFAULT_ALLOWED_ROOTS
    roots = [os.path.realpath(root) for root in raw.split(os.pathsep) if root]
    roots.append(artifacts_dir())
    return roots


def _within_allowed(path: str) -> bool:
    real = os.path.realpath(path)
    return any(
        real == root or real.startswith(root.rstrip(os.sep) + os.sep) for root in _allowed_roots()
    )


def _token() -> str:
    return secrets.token_hex(4)


def _opt_str(args: dict[str, Any], key: str, max_chars: int = MAX_ARG_CHARS) -> str | None:
    value = args.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ToolError("invalid_argument", f"{key} must be a string")
    if len(value) > max_chars:
        raise ToolError("invalid_argument", f"{key} exceeds {max_chars} characters")
    return value


def _choice(
    args: dict[str, Any], key: str, choices: tuple[str, ...], default: str | None = None
) -> str:
    value = args.get(key)
    if value is None:
        if default is None:
            raise ToolError("invalid_argument", f"{key} is required")
        return default
    if not isinstance(value, str) or value not in choices:
        raise ToolError("invalid_argument", f"{key} must be one of: {', '.join(choices)}")
    return value


def _name_arg(args: dict[str, Any]) -> str | None:
    name = _opt_str(args, "name", 100)
    if name is None:
        return None
    if not NAME_PATTERN.fullmatch(name):
        raise ToolError(
            "invalid_argument",
            "name must be 1-100 letters, digits, dots, underscores or hyphens, "
            "not starting with a dot",
        )
    return name


def _string_list(value: object, key: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_ARGS:
        raise ToolError("invalid_argument", f"{key} must be an array of at most {MAX_ARGS} strings")
    items: list[str] = []
    for item in value:
        if (
            not isinstance(item, str)
            or len(item) > MAX_ARG_CHARS
            or any(c in item for c in "\x00\r\n")
        ):
            raise ToolError(
                "invalid_argument",
                f"{key} items must be single-line strings of at most {MAX_ARG_CHARS} characters",
            )
        items.append(item)
    return items


@dataclass(frozen=True, slots=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str


def _binary(name: str) -> str:
    found = shutil.which(name)
    if found is None:
        raise ToolError("not_installed", f"{name} is not on PATH")
    return found


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait()


def _read_stdout(handle: IO[bytes]) -> str:
    size = handle.seek(0, os.SEEK_END)
    if size > PROCESS_OUTPUT_CAP_BYTES:
        raise ToolError(
            "output_too_large", f"process output exceeds {PROCESS_OUTPUT_CAP_BYTES} bytes"
        )
    handle.seek(0)
    return handle.read().decode("utf-8", errors="replace")


def _read_stderr(handle: IO[bytes]) -> str:
    handle.seek(0)
    return handle.read(DIAGNOSTIC_CHARS * 4).decode("utf-8", errors="replace")


def _run(argv: list[str], timeout: float) -> ProcessResult:
    """Run argv without a shell; kill the whole process group on timeout."""
    with tempfile.TemporaryFile() as out_file, tempfile.TemporaryFile() as err_file:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=out_file,
            stderr=err_file,
            start_new_session=True,
        )
        try:
            returncode = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            raise ToolError(
                "timeout", f"{os.path.basename(argv[0])} exceeded {timeout:g}s"
            ) from None
        return ProcessResult(returncode, _read_stdout(out_file), _read_stderr(err_file))


def _write_artifact(name: str, text: str) -> str:
    directory = artifacts_dir()
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def _json_out_path(args: dict[str, Any]) -> str | None:
    raw = _opt_str(args, "out_path", MAX_PATH_CHARS)
    if raw is None:
        return None
    if not os.path.isabs(raw) or not raw.endswith(".json") or not _within_allowed(raw):
        raise ToolError(
            "path_not_allowed", "out_path must be an absolute .json path under the allowed roots"
        )
    return os.path.realpath(raw)


def _findata_error(result: ProcessResult) -> ToolError:
    try:
        payload: object = json.loads(result.stdout)
    except ValueError:
        payload = None
    if isinstance(payload, dict) and payload.get("error") is not None:
        detail = json.dumps(payload["error"], ensure_ascii=False)
    else:
        detail = (
            result.stderr.strip() or result.stdout.strip() or f"exit status {result.returncode}"
        )
    return ToolError("findata_failed", detail[:DIAGNOSTIC_CHARS])


def _market_data(args: dict[str, Any]) -> str:
    command = _choice(args, "command", FINDATA_COMMANDS)
    findata_args = _string_list(args.get("args"), "args")
    if any(item == "--out" or item.startswith("--out=") for item in findata_args):
        raise ToolError(
            "invalid_argument", "--out is set by this tool; use out_path with command=kline"
        )
    if command != "kline" and args.get("out_path") is not None:
        raise ToolError("invalid_argument", "out_path only applies to command=kline")
    argv = [_binary("findata"), command, *findata_args]
    bars_file: str | None = None
    if command == "kline":
        bars_file = _json_out_path(args) or os.path.join(artifacts_dir(), f"kline-{_token()}.json")
        os.makedirs(os.path.dirname(bars_file), exist_ok=True)
        argv += ["--out", bars_file]
    result = _run(argv, FINDATA_TIMEOUT_SECONDS)
    if result.returncode != 0:
        raise _findata_error(result)
    stdout = result.stdout.strip()
    try:
        json.loads(stdout)
    except ValueError as exc:
        raise ToolError("bad_output", "findata printed non-JSON output") from exc
    body = stdout
    if len(body) > INLINE_TEXT_CHARS:
        saved = _write_artifact(f"market-{command}-{_token()}.json", body)
        body = f"full output saved to {saved}\n{body[:INLINE_TEXT_CHARS]}\n...[clipped]"
    if bars_file is not None:
        body = f"bars file (pass as kline_chart file_path): {bars_file}\n{body}"
    return body


def _load_bars_file(raw: str) -> object:
    if not os.path.isabs(raw) or not _within_allowed(raw):
        raise ToolError(
            "path_not_allowed", "file_path must be an absolute path under the allowed roots"
        )
    real = os.path.realpath(raw)
    if not os.path.isfile(real):
        raise ToolError("not_found", "file_path does not exist")
    if os.path.getsize(real) > INPUT_FILE_CAP_BYTES:
        raise ToolError("invalid_argument", "file_path is too large")
    try:
        with open(real, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        raise ToolError("invalid_argument", "file_path is not a readable JSON file") from exc


def _bars_input(args: dict[str, Any]) -> list[Any]:
    data = args.get("data")
    file_path = _opt_str(args, "file_path", MAX_PATH_CHARS)
    if (data is None) == (file_path is None):
        raise ToolError("invalid_argument", "provide exactly one of data or file_path")
    bars: object = data if file_path is None else _load_bars_file(file_path)
    if (
        not isinstance(bars, list)
        or not bars
        or len(bars) > MAX_BARS
        or not all(isinstance(bar, dict) for bar in bars)
    ):
        raise ToolError(
            "invalid_argument",
            f"bars must be a non-empty array of at most {MAX_BARS} OHLCV objects",
        )
    return bars


def _ma_arg(value: object) -> list[int]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_MA_PERIODS:
        raise ToolError("invalid_argument", f"ma must be an array of 1-{MAX_MA_PERIODS} periods")
    for period in value:
        if isinstance(period, bool) or not isinstance(period, int) or not 1 <= period <= 250:
            raise ToolError("invalid_argument", "ma periods must be integers from 1 to 250")
    return list(value)


def _markers_arg(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > MAX_MARKERS:
        raise ToolError(
            "invalid_argument", f"markers must be an array of at most {MAX_MARKERS} items"
        )
    markers: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ToolError("invalid_argument", "markers items must be objects")
        price = item.get("price")
        if (
            isinstance(price, bool)
            or not isinstance(price, (int, float))
            or not math.isfinite(price)
        ):
            raise ToolError("invalid_argument", "marker price must be a finite number")
        if item.get("type") not in MARKER_TYPES:
            raise ToolError("invalid_argument", "marker type must be support or resistance")
        marker: dict[str, Any] = {"price": price, "type": item["type"]}
        label = item.get("label")
        if label is not None:
            if not isinstance(label, str) or len(label) > MAX_ARG_CHARS:
                raise ToolError("invalid_argument", "marker label must be a short string")
            marker["label"] = label
        markers.append(marker)
    return markers


def _forecast_arg(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ToolError("invalid_argument", "forecast must be an object")
    start = value.get("start")
    end = value.get("end")
    series = value.get("series")
    if not isinstance(start, str) or not isinstance(end, str):
        raise ToolError(
            "invalid_argument", "forecast.start and forecast.end must be YYYY-MM-DD strings"
        )
    if not isinstance(series, list) or not 1 <= len(series) <= MAX_FORECAST_SERIES:
        raise ToolError(
            "invalid_argument", f"forecast.series must hold 1-{MAX_FORECAST_SERIES} values"
        )
    label = value.get("label", FORECAST_LABEL)
    if not isinstance(label, str):
        raise ToolError("invalid_argument", "forecast.label must be a string")
    return {"label": label, "start": start, "end": end, "series": series}


def _kline_markup(args: dict[str, Any], bars: list[Any]) -> str:
    props: dict[str, Any] = {
        "data": bars,
        "colors": _choice(args, "colors", COLOR_MODES, "intl"),
    }
    for key in ("title", "symbol"):
        value = _opt_str(args, key)
        if value:
            props[key] = value
    if args.get("ma") is not None:
        props["ma"] = _ma_arg(args["ma"])
    if args.get("markers") is not None:
        props["markers"] = _markers_arg(args["markers"])
    if args.get("forecast") is not None:
        props["forecast"] = _forecast_arg(args["forecast"])
    try:
        attrs = " ".join(
            f"{key}={{{json.dumps(value, ensure_ascii=False, allow_nan=False)}}}"
            for key, value in props.items()
        )
    except ValueError as exc:
        raise ToolError("invalid_argument", "numeric values must be finite") from exc
    return f"<KLine {attrs} />"


def _png_size(path: str) -> tuple[int, int] | None:
    try:
        with open(path, "rb") as handle:
            head = handle.read(24)
    except OSError:
        return None
    if len(head) < 24 or head[:8] != PNG_SIGNATURE or head[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", head[16:24])
    return width, height


def _render(source: str, name: str, fmt: str, notes: list[str]) -> str:
    """Run richdoc on a Markdown file. Richdoc errors still list written files."""
    ext = "pdf" if fmt == "pdf" else "png"
    target = os.path.join(artifacts_dir(), f"{name}.{ext}")
    os.makedirs(artifacts_dir(), exist_ok=True)
    result = _run([_binary("richdoc"), source, target, "--format", fmt], RICHDOC_TIMEOUT_SECONDS)
    if result.returncode != 0:
        detail = (
            result.stderr.strip() or result.stdout.strip() or f"exit status {result.returncode}"
        )
        raise ToolError("render_failed", detail[:DIAGNOSTIC_CHARS])
    try:
        payload: Any = json.loads(result.stdout)
    except ValueError as exc:
        raise ToolError("render_failed", "richdoc printed non-JSON output") from exc
    files = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(files, list) or not all(isinstance(path, str) for path in files):
        raise ToolError("render_failed", "richdoc returned no file list")
    errors = payload.get("errors") or []
    lines = [f"rendered {len(files)} file(s), pages={payload.get('page_count')}"]
    for path in files:
        size = _png_size(path) if path.endswith(".png") else None
        suffix = f" ({size[0]}x{size[1]}px)" if size else ""
        lines.append(f"- {path}{suffix}")
    lines.extend(notes)
    lines.append("Deliver these paths with the bridge report_progress files argument.")
    if errors:
        lines.append("richdoc errors:")
        lines.extend(f"- {str(error)[:DIAGNOSTIC_CHARS]}" for error in errors)
        raise ToolError("render_errors", "\n".join(lines))
    return "\n".join(lines)


def _kline_chart(args: dict[str, Any]) -> str:
    bars = _bars_input(args)
    name = _name_arg(args) or f"kline-{_token()}"
    source = _write_artifact(f"{name}.md", _kline_markup(args, bars) + "\n")
    notes: list[str] = []
    if len(bars) > RICHDOC_BAR_LIMIT:
        notes.append(f"richdoc draws the latest {RICHDOC_BAR_LIMIT} of {len(bars)} bars")
    return _render(source, name, "png", notes)


def _checked_markdown_path(raw: str) -> str:
    if not os.path.isabs(raw) or not _within_allowed(raw):
        raise ToolError(
            "path_not_allowed", "markdown_path must be an absolute path under the allowed roots"
        )
    real = os.path.realpath(raw)
    if not os.path.isfile(real):
        raise ToolError("not_found", "markdown_path does not exist")
    if os.path.getsize(real) > MAX_MARKDOWN_CHARS:
        raise ToolError("invalid_argument", "markdown_path is too large")
    return real


def _render_report(args: dict[str, Any]) -> str:
    markdown = _opt_str(args, "markdown", MAX_MARKDOWN_CHARS)
    markdown_path = _opt_str(args, "markdown_path", MAX_PATH_CHARS)
    if (markdown is None) == (markdown_path is None):
        raise ToolError("invalid_argument", "provide exactly one of markdown or markdown_path")
    fmt = _choice(args, "format", RENDER_FORMATS, "png")
    name = _name_arg(args) or f"report-{_token()}"
    if markdown_path is not None:
        source = _checked_markdown_path(markdown_path)
    else:
        if not markdown or not markdown.strip():
            raise ToolError("invalid_argument", "markdown must not be empty")
        source = _write_artifact(f"{name}.md", markdown)
    return _render(source, name, fmt, [])


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], str]

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


MARKET_DATA_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "command": {
            "type": "string",
            "enum": list(FINDATA_COMMANDS),
            "description": (
                "findata subcommand: resolve, quote, kline, overview, sectors, fund or batch."
            ),
        },
        "args": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": MAX_ARGS,
            "description": (
                "Arguments after the subcommand, one token per item, e.g. "
                "['600519', '--market', 'CN', '--limit', '120']. Do not include --out."
            ),
        },
        "out_path": {
            "type": "string",
            "description": (
                "command=kline only: absolute .json path for the bars array. Default is generated."
            ),
        },
    },
    "required": ["command"],
    "additionalProperties": False,
}

KLINE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "data": {
            "type": "array",
            "items": {"type": "object"},
            "maxItems": MAX_BARS,
            "description": (
                "OHLCV rows, oldest first: {date:'YYYY-MM-DD', open, high, low, close, volume}."
            ),
        },
        "file_path": {
            "type": "string",
            "description": (
                "Absolute path to a JSON array of OHLCV rows, "
                "e.g. the bars file from market_data kline."
            ),
        },
        "title": {"type": "string"},
        "symbol": {"type": "string"},
        "ma": {
            "type": "array",
            "items": {"type": "integer", "minimum": 1, "maximum": 250},
            "maxItems": MAX_MA_PERIODS,
            "description": "Moving-average periods. Default 5, 10, 20, 60.",
        },
        "colors": {
            "type": "string",
            "enum": list(COLOR_MODES),
            "description": (
                "intl (default): up green, down red. cn only when the user explicitly asks."
            ),
        },
        "markers": {
            "type": "array",
            "maxItems": MAX_MARKERS,
            "items": {
                "type": "object",
                "properties": {
                    "price": {"type": "number"},
                    "type": {"type": "string", "enum": list(MARKER_TYPES)},
                    "label": {"type": "string"},
                },
                "required": ["price", "type"],
                "additionalProperties": False,
            },
        },
        "forecast": {
            "type": "object",
            "description": (
                "Projection after the last bar, drawn dashed over a shaded band "
                "and labelled as forecast. Use only for forecasts, never for measured data."
            ),
            "properties": {
                "label": {
                    "type": "string",
                    "description": "Must contain 非实测. Default 预测(非实测).",
                },
                "start": {
                    "type": "string",
                    "description": "First forecast date, YYYY-MM-DD, after the last bar.",
                },
                "end": {"type": "string", "description": "Last forecast date, YYYY-MM-DD."},
                "series": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_FORECAST_SERIES,
                    "items": {},
                    "description": "Forecast closes as numbers, or {date, close} objects.",
                },
            },
            "required": ["start", "end", "series"],
            "additionalProperties": False,
        },
        "name": {
            "type": "string",
            "description": "Output basename: letters, digits, dots, underscores or hyphens.",
        },
    },
    "additionalProperties": False,
}

RENDER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "markdown": {
            "type": "string",
            "description": "Report Markdown source with optional <KLine/> tags.",
        },
        "markdown_path": {
            "type": "string",
            "description": (
                "Absolute path to a Markdown file under the allowed roots "
                "(/workspace, /home/agent)."
            ),
        },
        "name": {
            "type": "string",
            "description": "Output basename: letters, digits, dots, underscores or hyphens.",
        },
        "format": {
            "type": "string",
            "enum": list(RENDER_FORMATS),
            "description": "png (default long image), pdf, or both.",
        },
    },
    "additionalProperties": False,
}

TOOLS: dict[str, Tool] = {
    tool.name: tool
    for tool in (
        Tool(
            name="market_data",
            description=(
                "ALWAYS use this for stock, index, fund, sector and quote data instead of "
                "hand-written HTTP or scraping. Runs the preinstalled findata CLI. "
                "Pass the subcommand and arguments; for kline the bars are written to a JSON "
                "array reported as the bars file. Pass that path to kline_chart file_path, "
                "never the printed envelope. Cite the printed source, last trading date "
                "and adjustment basis."
            ),
            input_schema=MARKET_DATA_SCHEMA,
            handler=_market_data,
        ),
        Tool(
            name="kline_chart",
            description=(
                "ALWAYS use this to draw OHLCV K-line candlestick charts as PNG instead of "
                "matplotlib or hand-drawn charts. Pass data rows or file_path (the bars file "
                "from market_data kline). Green is up and red is down by default. Set forecast "
                "only for projections: it is drawn dashed, shaded and labelled as non-measured. "
                "Returns PNG paths. Deliver them to the user with the bridge report_progress "
                "files argument."
            ),
            input_schema=KLINE_SCHEMA,
            handler=_kline_chart,
        ),
        Tool(
            name="render_report",
            description=(
                "ALWAYS use this to render Markdown reports to PNG long images (default) or PDF "
                "with richdoc, instead of hand-written HTML or matplotlib. Pass markdown text or "
                "markdown_path. Embed charts with <KLine/> tags as the rich-report skill "
                "describes. Returns file paths; deliver them to the user with the bridge "
                "report_progress files argument. Errors return isError; fix the Markdown "
                "and render again."
            ),
            input_schema=RENDER_SCHEMA,
            handler=_render_report,
        ),
    )
}


def run_tool(tool: Tool, arguments: dict[str, Any]) -> tuple[str, bool]:
    """Run a tool; every failure becomes ``(text, True)``, never a JSON-RPC error."""
    try:
        return tool.handler(arguments), False
    except ToolError as exc:
        error = exc
    except Exception as exc:  # a tool bug must not take the server down
        log(f"tool {tool.name} crashed: {type(exc).__name__}")
        error = ToolError("internal", type(exc).__name__)
    log(f"tool {tool.name} failed: {error.code}")
    return error.render(), True


def _ok(request_id: object, result: dict[str, Any], modern: bool) -> dict[str, Any]:
    if modern:
        result.setdefault("resultType", "complete")
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(
    request_id: object, code: int, message: str, data: dict[str, Any] | None = None
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _is_modern(params: dict[str, Any]) -> bool:
    """True for per-request ``_meta`` protocol negotiation; rejects unsupported versions."""
    meta = params.get("_meta")
    if not isinstance(meta, dict) or META_PROTOCOL_KEY not in meta:
        return False
    requested = meta[META_PROTOCOL_KEY]
    if requested != PROTOCOL_MODERN:
        raise RpcError(
            UNSUPPORTED_VERSION,
            "Unsupported protocol version",
            {"requested": requested, "supported": [PROTOCOL_MODERN]},
        )
    return True


def _tool_call(params: dict[str, Any]) -> tuple[Tool, dict[str, Any]]:
    name = params.get("name")
    if not isinstance(name, str) or name not in TOOLS:
        raise RpcError(INVALID_PARAMS, f"unknown tool: {name}")
    arguments = params.get("arguments")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise RpcError(INVALID_PARAMS, "arguments must be an object")
    return TOOLS[name], arguments


def _dispatch(method: str, params: dict[str, Any]) -> dict[str, Any]:
    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if requested in LEGACY_VERSIONS else LEGACY_VERSIONS[0]
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": INSTRUCTIONS,
        }
    if method == "server/discover":
        return {
            "resultType": "complete",
            "supportedVersions": [PROTOCOL_MODERN],
            "capabilities": {"tools": {}},
            "_meta": {"io.modelcontextprotocol/serverInfo": SERVER_INFO},
            "instructions": INSTRUCTIONS,
            "ttlMs": 3600000,
            "cacheScope": "public",
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": [tool.describe() for tool in TOOLS.values()]}
    raise RpcError(METHOD_NOT_FOUND, f"method not found: {method}")


class Session:
    """One stdio session: answers requests inline, runs ``tools/call`` on the pool."""

    def __init__(self, pool: concurrent.futures.ThreadPoolExecutor) -> None:
        self._pool = pool
        self._lock = threading.Lock()

    def handle_line(self, raw: bytes) -> None:
        if not raw.strip():
            return
        try:
            msg = json.loads(raw)
        except ValueError:
            self._send(_error(None, PARSE_ERROR, "parse error"))
            return
        if not isinstance(msg, dict):
            self._send(_error(None, INVALID_REQUEST, "batches and non-objects are not supported"))
            return
        method = msg.get("method")
        if method is None:
            if "result" not in msg and "error" not in msg:
                self._send(_error(msg.get("id"), INVALID_REQUEST, "missing method"))
            return
        if not isinstance(method, str):
            self._send(_error(msg.get("id"), INVALID_REQUEST, "method must be a string"))
            return
        if "id" not in msg:
            return  # notifications are accepted silently and never answered
        request_id = msg["id"]
        try:
            params = msg.get("params")
            if params is None:
                params = {}
            if not isinstance(params, dict):
                raise RpcError(INVALID_PARAMS, "params must be an object")
            modern = _is_modern(params)
            if method == "tools/call":
                tool, arguments = _tool_call(params)
                self._pool.submit(self._run_tool, request_id, modern, tool, arguments)
                return
            self._send(_ok(request_id, _dispatch(method, params), modern))
        except RpcError as exc:
            self._send(_error(request_id, exc.code, exc.message, exc.data))

    def _run_tool(
        self, request_id: object, modern: bool, tool: Tool, arguments: dict[str, Any]
    ) -> None:
        text, is_error = run_tool(tool, arguments)
        result = {"content": [{"type": "text", "text": text}], "isError": is_error}
        self._send(_ok(request_id, result, modern))

    def _send(self, message: dict[str, Any]) -> None:
        line = json.dumps(message, separators=(",", ":")) + "\n"
        with self._lock:
            sys.stdout.write(line)
            sys.stdout.flush()


def main() -> int:
    log(f"started artifacts={artifacts_dir()}")
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=TOOL_WORKERS, thread_name_prefix="report-tool"
    ) as pool:
        session = Session(pool)
        for raw in sys.stdin.buffer:
            session.handle_line(raw)
    log("stdin closed; exiting")
    return 0


if __name__ == "__main__":
    sys.exit(main())
