#!/usr/bin/env python3
"""cgbot bridge: in-sandbox MCP server (stdio, newline-delimited JSON-RPC) launched by Codex.

Tools: ``report_progress``, ``send_file``, ``generate_image``, ``web_search``, ``gemini_search``.
Each talks to the bot's ``/bridge`` HTTP API; file bytes travel through the chunked upload
pipeline, never fully buffered.

Environment:
    CGBOT_BRIDGE_URL            required, e.g. ``http://cgbot:8000/bridge``
    CGBOT_BRIDGE_TOKEN          required, sent as ``Authorization: Bearer <token>``
    CGBOT_USER_ID               optional, logged as an id at startup only
    CGBOT_BRIDGE_ALLOWED_ROOTS  optional, os.pathsep-separated; default ``/workspace:/home/agent``

stdout carries JSON-RPC messages only; diagnostics go to stderr as ids and counts, never bodies
or tokens. Stdlib only.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import http.client
import json
import os
import stat
import sys
import threading
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

CHUNK_BYTES = 2 * 1024 * 1024
MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_MESSAGE_CHARS = 8000
MAX_FILES = 16
MAX_REFERENCE_IMAGES = 8
HASH_BLOCK_BYTES = 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 60.0
IMAGEGEN_TIMEOUT_SECONDS = 900.0
SEARCH_TIMEOUT_SECONDS = 120.0
GEMINI_SEARCH_TIMEOUT_SECONDS = 180.0
SEARCH_FRESHNESS = ("day", "week", "month", "year")
CHUNK_ATTEMPTS = 3
CHUNK_RETRY_DELAYS: tuple[float, ...] = (0.5, 1.0)
ERROR_SNIPPET_CHARS = 200
DEFAULT_ALLOWED_ROOTS = os.pathsep.join(("/workspace", "/home/agent"))
TOOL_WORKERS = 4
TRANSPORT_ERRORS = (OSError, http.client.HTTPException)
STATUSES = ("running", "waiting_user_input", "completed", "failed")
THREAD_ID_KEYS = ("threadId", "thread_id")
TURN_METADATA_KEY = "x-codex-turn-metadata"
THREAD_ID_MAX_CHARS = 256  # mirrors the bridge's limit; longer ids are dropped, not sent

PROTOCOL_MODERN = "2026-07-28"
LEGACY_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
META_PROTOCOL_KEY = "io.modelcontextprotocol/protocolVersion"
SERVER_INFO = {"name": "cgbot-bridge", "version": "1.0.0"}
INSTRUCTIONS = (
    "cgbot bridge: report_progress and send_file deliver messages and files to the user; "
    "generate_image creates images; web_search and gemini_search search through the bot. "
    "Paths must be absolute files under /workspace or /home/agent."
)

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
UNSUPPORTED_VERSION = -32022


def log(message: str) -> None:
    print(f"cgbot-bridge: {message}", file=sys.stderr, flush=True)


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


class BridgeHTTPError(Exception):
    """Non-2xx response from the bridge."""

    def __init__(self, status: int, body: bytes) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status
        self.body = body

    def to_tool_error(self) -> ToolError:
        try:
            data = json.loads(self.body)
        except ValueError:
            data = None
        if (
            isinstance(data, dict)
            and isinstance(data.get("error"), str)
            and isinstance(data.get("message"), str)
        ):
            return ToolError(data["error"], data["message"])
        snippet = self.body.decode("utf-8", "replace")[:ERROR_SNIPPET_CHARS]
        return ToolError(f"http_{self.status}", snippet)


class _FileChunk:
    """Read-only view of one byte range of an open fd; http.client streams it via ``read``."""

    def __init__(self, fd: int, offset: int, length: int) -> None:
        self._fd = fd
        self._offset = offset
        self._length = length
        self._pos = 0

    def __len__(self) -> int:
        return self._length

    def read(self, n: int = -1) -> bytes:
        remaining = self._length - self._pos
        if remaining <= 0:
            return b""
        want = remaining if n < 0 else min(n, remaining)
        data = os.pread(self._fd, want, self._offset + self._pos)
        if not data:
            raise ToolError("file_changed", "file shrank during upload")
        self._pos += len(data)
        return data


class BridgeClient:
    """Blocking http.client wrapper for the bridge API. Proxy env is ignored by design."""

    def __init__(self, base_url: str, token: str) -> None:
        parts = urllib.parse.urlsplit(base_url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("expected an http(s) URL with a host")
        self._https = parts.scheme == "https"
        self._host = parts.hostname
        self._port = parts.port
        self._prefix = parts.path.rstrip("/")
        self._auth = f"Bearer {token}"

    def _call(
        self,
        method: str,
        path: str,
        body: bytes | _FileChunk | None,
        headers: dict[str, str],
        timeout: float,
    ) -> bytes:
        conn_cls = http.client.HTTPSConnection if self._https else http.client.HTTPConnection
        conn = conn_cls(self._host, self._port, timeout=timeout)
        try:
            conn.request(
                method,
                self._prefix + path,
                body=body,
                headers={"Authorization": self._auth, **headers},
            )
            resp = conn.getresponse()
            data = resp.read()
        finally:
            conn.close()
        if not 200 <= resp.status < 300:
            raise BridgeHTTPError(resp.status, data)
        return data

    def post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        data = self._call(
            "POST",
            path,
            json.dumps(payload).encode("utf-8"),
            {"Content-Type": "application/json"},
            REQUEST_TIMEOUT_SECONDS,
        )
        try:
            parsed = json.loads(data)
        except ValueError:
            parsed = None
        if not isinstance(parsed, dict):
            raise ToolError("bad_response", f"bridge returned a non-object body for {path}")
        return parsed

    def post_text(self, path: str, payload: dict[str, Any], timeout: float) -> str:
        data = self._call(
            "POST",
            path,
            json.dumps(payload).encode("utf-8"),
            {"Content-Type": "application/json"},
            timeout,
        )
        return data.decode("utf-8", "replace")

    def put_chunk(self, upload_id: str, index: int, chunk: _FileChunk, length: int) -> None:
        self._call(
            "PUT",
            _upload_path(upload_id, f"/chunks/{index}"),
            chunk,
            {"Content-Type": "application/octet-stream", "Content-Length": str(length)},
            REQUEST_TIMEOUT_SECONDS,
        )


def _upload_path(upload_id: str, suffix: str) -> str:
    return f"/uploads/{urllib.parse.quote(upload_id, safe='')}{suffix}"


def allowed_roots() -> list[str]:
    raw = os.environ.get("CGBOT_BRIDGE_ALLOWED_ROOTS", DEFAULT_ALLOWED_ROOTS)
    return [os.path.realpath(root) for root in raw.split(os.pathsep) if root]


def _is_within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


@dataclass(frozen=True, slots=True)
class SandboxFile:
    resolved: str
    name: str


def check_file(requested: object) -> SandboxFile:
    """Validate a sandbox path without touching the network.

    Symlinks resolve before the root check, so an escaping link is refused as forbidden_path.
    """
    if not isinstance(requested, str) or not requested or not os.path.isabs(requested):
        raise ToolError("invalid_argument", "path must be a non-empty absolute string")
    resolved = os.path.realpath(requested)
    if not any(_is_within(resolved, root) for root in allowed_roots()):
        raise ToolError("forbidden_path", f"{requested} is outside the allowed roots")
    if not os.path.isfile(resolved):
        raise ToolError("not_found", f"{requested} is not a regular file")
    size = os.stat(resolved).st_size
    if size > MAX_FILE_BYTES:
        raise ToolError("too_large", f"file is {size} bytes; limit is {MAX_FILE_BYTES}")
    name = os.path.basename(requested.rstrip(os.sep)) or os.path.basename(resolved)
    return SandboxFile(resolved, name)


def _open_regular(path: str) -> int:
    fd = os.open(path, os.O_RDONLY)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ToolError("not_found", "path is not a regular file")
    return fd


def _sha256_of(fd: int, size: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while offset < size:
        block = os.pread(fd, min(HASH_BLOCK_BYTES, size - offset), offset)
        if not block:
            raise ToolError("file_changed", "file shrank while hashing")
        digest.update(block)
        offset += len(block)
    return digest.hexdigest()


def _put_chunk_with_retry(
    client: BridgeClient, upload_id: str, index: int, fd: int, offset: int, length: int
) -> None:
    """Retry only chunk PUTs: connection errors, timeouts and 5xx. Never 4xx."""
    last = CHUNK_ATTEMPTS - 1
    for attempt in range(CHUNK_ATTEMPTS):
        try:
            client.put_chunk(upload_id, index, _FileChunk(fd, offset, length), length)
            return
        except BridgeHTTPError as exc:
            if exc.status < 500 or attempt == last:
                raise
        except TRANSPORT_ERRORS:
            if attempt == last:
                raise
        time.sleep(CHUNK_RETRY_DELAYS[attempt])


def upload_file(client: BridgeClient, target: SandboxFile) -> dict[str, Any]:
    """Create → chunk PUTs → complete. Returns the bridge's complete body (upload_id, kind, ...)."""
    fd = _open_regular(target.resolved)
    try:
        size = os.fstat(fd).st_size
        if size > MAX_FILE_BYTES:
            raise ToolError("too_large", f"file is {size} bytes; limit is {MAX_FILE_BYTES}")
        digest = _sha256_of(fd, size)
        created = client.post_json("/uploads", {"name": target.name, "size": size})
        upload_id = created.get("upload_id")
        if not isinstance(upload_id, str) or not upload_id:
            raise ToolError("bad_response", "bridge returned no upload_id")
        for index in range((size + CHUNK_BYTES - 1) // CHUNK_BYTES):
            offset = index * CHUNK_BYTES
            length = min(CHUNK_BYTES, size - offset)
            _put_chunk_with_retry(client, upload_id, index, fd, offset, length)
        body = {"size": size, "sha256": digest}
        return client.post_json(_upload_path(upload_id, "/complete"), body)
    finally:
        os.close(fd)


def _post_report(
    client: BridgeClient,
    message: str,
    status: str,
    uploads: list[dict[str, Any]],
    thread_id: str | None,
) -> None:
    payload: dict[str, Any] = {
        "message": message,
        "status": status,
        "files": [upload["upload_id"] for upload in uploads],
    }
    if thread_id is not None:
        payload["thread_id"] = thread_id
    client.post_json("/report", payload)


def _str_arg(
    args: dict[str, Any], key: str, *, required: bool = False, max_chars: int | None = None
) -> str:
    """Return the string argument, or "" when an optional argument is absent."""
    value = args.get(key)
    if value is None:
        if required:
            raise ToolError("invalid_argument", f"{key} is required")
        return ""
    if not isinstance(value, str):
        raise ToolError("invalid_argument", f"{key} must be a string")
    if required and not value.strip():
        raise ToolError("invalid_argument", f"{key} must not be empty")
    if max_chars is not None and len(value) > max_chars:
        raise ToolError("invalid_argument", f"{key} exceeds {max_chars} characters")
    return value


def _list_arg(args: dict[str, Any], key: str, limit: int) -> list[object]:
    value = args.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise ToolError("invalid_argument", f"{key} must be an array")
    if len(value) > limit:
        raise ToolError("invalid_argument", f"{key} allows at most {limit} items")
    return value


def _report_progress(client: BridgeClient, args: dict[str, Any], thread_id: str | None) -> str:
    message = _str_arg(args, "message", required=True, max_chars=MAX_MESSAGE_CHARS)
    status = _str_arg(args, "status") or "running"
    if status not in STATUSES:
        raise ToolError("invalid_argument", f"status must be one of {', '.join(STATUSES)}")
    targets = [check_file(path) for path in _list_arg(args, "files", MAX_FILES)]
    uploads = [upload_file(client, target) for target in targets]
    _post_report(client, message, status, uploads, thread_id)
    return f"reported: status={status} files={len(uploads)}"


def _send_file(client: BridgeClient, args: dict[str, Any], thread_id: str | None) -> str:
    target = check_file(args.get("path"))
    caption = _str_arg(args, "caption", max_chars=MAX_MESSAGE_CHARS)
    uploaded = upload_file(client, target)
    _post_report(client, caption, "running", [uploaded], thread_id)
    return f"sent: {uploaded['name']} ({uploaded['kind']}, {uploaded['size']} bytes)"


def _generate_image(client: BridgeClient, args: dict[str, Any], thread_id: str | None) -> str:
    payload: dict[str, Any] = {"prompt": _str_arg(args, "prompt", required=True)}
    for key in ("size", "name"):
        value = _str_arg(args, key)
        if value:
            payload[key] = value
    refs = _list_arg(args, "reference_images", MAX_REFERENCE_IMAGES)
    if refs:
        payload["reference_images"] = [check_file(path).resolved for path in refs]
    return client.post_text("/imagegen", payload, IMAGEGEN_TIMEOUT_SECONDS)


def _web_search(client: BridgeClient, args: dict[str, Any], thread_id: str | None) -> str:
    payload: dict[str, Any] = {
        "query": _str_arg(args, "query", required=True, max_chars=200)
    }
    engine = _str_arg(args, "engine")
    if engine:
        payload["engine"] = engine
    if "count" in args:
        count = args["count"]
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ToolError("invalid_argument", "count must be a positive integer")
        payload["count"] = count
    freshness = _str_arg(args, "freshness")
    if freshness:
        if freshness not in SEARCH_FRESHNESS:
            raise ToolError("invalid_argument", "freshness must be day, week, month or year")
        payload["freshness"] = freshness
    return client.post_text("/search", payload, SEARCH_TIMEOUT_SECONDS)


def _gemini_search(client: BridgeClient, args: dict[str, Any], thread_id: str | None) -> str:
    payload: dict[str, Any] = {
        "query": _str_arg(args, "query", required=True, max_chars=300)
    }
    recency = _str_arg(args, "recency")
    if recency:
        if recency not in SEARCH_FRESHNESS:
            raise ToolError("invalid_argument", "recency must be day, week, month or year")
        payload["recency"] = recency
    return client.post_text("/gemini-search", payload, GEMINI_SEARCH_TIMEOUT_SECONDS)


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[BridgeClient, dict[str, Any], str | None], str]

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


TOOLS: dict[str, Tool] = {
    tool.name: tool
    for tool in (
        Tool(
            "report_progress",
            "Send a progress message to the user, optionally with files (absolute paths).",
            {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_MESSAGE_CHARS,
                        "description": "Text shown to the user.",
                    },
                    "status": {
                        "type": "string",
                        "enum": list(STATUSES),
                        "default": "running",
                    },
                    "files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": MAX_FILES,
                        "description": "Absolute paths of files to attach.",
                    },
                },
                "required": ["message"],
            },
            _report_progress,
        ),
        Tool(
            "send_file",
            "Send one file to the user with an optional caption.",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "minLength": 1, "description": "Absolute path."},
                    "caption": {"type": "string", "maxLength": MAX_MESSAGE_CHARS},
                },
                "required": ["path"],
            },
            _send_file,
        ),
        Tool(
            "generate_image",
            "Generate an image from a prompt through the bot; returns the bridge's JSON result.",
            {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "minLength": 1},
                    "size": {"type": "string"},
                    "name": {"type": "string"},
                    "reference_images": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": MAX_REFERENCE_IMAGES,
                        "description": "Absolute paths of reference images.",
                    },
                },
                "required": ["prompt"],
            },
            _generate_image,
        ),
        Tool(
            "web_search",
            "Search the web through the bot's configured engines; returns source text and URLs.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 200},
                    "engine": {
                        "type": "string",
                        "description": "Optional configured engine name; omit for the bot default.",
                    },
                    "count": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "Result count, subject to the bot's configured maximum.",
                    },
                    "freshness": {"type": "string", "enum": list(SEARCH_FRESHNESS)},
                },
                "required": ["query"],
            },
            _web_search,
        ),
        Tool(
            "gemini_search",
            "Search through the bot's Gemini backend; returns a grounded answer with sources.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 300},
                    "recency": {"type": "string", "enum": list(SEARCH_FRESHNESS)},
                },
                "required": ["query"],
            },
            _gemini_search,
        ),
    )
}


def run_tool(
    tool: Tool, client: BridgeClient, arguments: dict[str, Any], thread_id: str | None
) -> tuple[str, bool]:
    """Run a tool; every failure becomes ``(text, True)``, never a JSON-RPC error."""
    try:
        return tool.handler(client, arguments, thread_id), False
    except ToolError as exc:
        error = exc
    except BridgeHTTPError as exc:
        error = exc.to_tool_error()
    except TRANSPORT_ERRORS as exc:
        error = ToolError("unreachable", type(exc).__name__)
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


def _valid_thread_id(value: object) -> str | None:
    if isinstance(value, str) and 0 < len(value) <= THREAD_ID_MAX_CHARS:
        return value
    return None


def _thread_id(params: dict[str, Any]) -> str | None:
    """Thread id from ``tools/call`` ``_meta``: top-level ``threadId``/``thread_id`` first, then
    ``x-codex-turn-metadata.thread_id``. Values the bridge would reject are dropped."""
    meta = params.get("_meta")
    if not isinstance(meta, dict):
        return None
    for key in THREAD_ID_KEYS:
        found = _valid_thread_id(meta.get(key))
        if found is not None:
            return found
    turn_meta = meta.get(TURN_METADATA_KEY)
    if isinstance(turn_meta, dict):
        return _valid_thread_id(turn_meta.get("thread_id"))
    return None


def _tool_call(params: dict[str, Any]) -> tuple[Tool, dict[str, Any], str | None]:
    name = params.get("name")
    if not isinstance(name, str) or name not in TOOLS:
        raise RpcError(INVALID_PARAMS, f"unknown tool: {name}")
    arguments = params.get("arguments")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise RpcError(INVALID_PARAMS, "arguments must be an object")
    return TOOLS[name], arguments, _thread_id(params)


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

    def __init__(self, client: BridgeClient, pool: concurrent.futures.ThreadPoolExecutor) -> None:
        self._client = client
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
                tool, arguments, thread_id = _tool_call(params)
                self._pool.submit(self._run_tool, request_id, modern, tool, arguments, thread_id)
                return
            self._send(_ok(request_id, _dispatch(method, params), modern))
        except RpcError as exc:
            self._send(_error(request_id, exc.code, exc.message, exc.data))

    def _run_tool(
        self,
        request_id: object,
        modern: bool,
        tool: Tool,
        arguments: dict[str, Any],
        thread_id: str | None,
    ) -> None:
        text, is_error = run_tool(tool, self._client, arguments, thread_id)
        result = {"content": [{"type": "text", "text": text}], "isError": is_error}
        self._send(_ok(request_id, result, modern))

    def _send(self, message: dict[str, Any]) -> None:
        line = json.dumps(message, separators=(",", ":")) + "\n"
        with self._lock:
            sys.stdout.write(line)
            sys.stdout.flush()


def main() -> int:
    base_url = os.environ.get("CGBOT_BRIDGE_URL", "").strip()
    token = os.environ.get("CGBOT_BRIDGE_TOKEN", "").strip()
    missing = [
        name
        for name, value in (("CGBOT_BRIDGE_URL", base_url), ("CGBOT_BRIDGE_TOKEN", token))
        if not value
    ]
    if missing:
        log(f"missing required environment: {', '.join(missing)}")
        return 2
    try:
        client = BridgeClient(base_url, token)
    except ValueError as exc:
        log(f"invalid CGBOT_BRIDGE_URL: {exc}")
        return 2
    log(f"started user={os.environ.get('CGBOT_USER_ID') or 'unset'}")
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=TOOL_WORKERS, thread_name_prefix="bridge-tool"
    ) as pool:
        session = Session(client, pool)
        for raw in sys.stdin.buffer:
            session.handle_line(raw)
    log("stdin closed; exiting")
    return 0


if __name__ == "__main__":
    sys.exit(main())
