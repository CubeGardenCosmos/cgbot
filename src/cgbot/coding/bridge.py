"""Bridge server (M5 slice E): sandbox -> bot transport mounted under ``/bridge``.

Routes (everything except ``files`` needs ``Authorization: Bearer <sandbox token>``):

* ``POST /bridge/uploads`` -- open a chunked upload ``{name, size}`` -> ``{upload_id}``
* ``PUT /bridge/uploads/{upload_id}/chunks/{index}`` -- raw chunk, sequential 0-based indices
* ``POST /bridge/uploads/{upload_id}/complete`` -- verify, sniff type ->
  ``{name, mime, size, kind}``
* ``POST /bridge/report`` -- ``{message, status?, files?, thread_id?}`` -> ``CodingNotice`` (kind
  ``report``); the user id always comes from the bearer token
* ``GET /bridge/files/{token}/{name}`` -- unauthenticated, token-scoped download (NapCat fetches)

Bytes live under ``bridge.temp_dir``. A partial upload expires ``file_ttl_seconds`` after its last
activity; a completed file expires that long after completion. The sweeper removes expired records
together with their bytes, and orphaned directories (e.g. left by a restart) once they are older
than the TTL.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast, get_args

from aiohttp import web

from cgbot.coding.config import CodingConfig
from cgbot.coding.contracts import (
    CodingNotice,
    CodingNotifier,
    FileKind,
    ReportStatus,
    SandboxAuth,
    StoredFile,
)

log = logging.getLogger(__name__)

REPORT_MAX_CHARS = 8000
REPORT_MAX_FILES = 16
REPORT_MAX_BODY = 64 * 1024
THREAD_ID_MAX_CHARS = 256
UPLOAD_NAME_MAX_CHARS = 120
SMALL_JSON_LIMIT = 4096
READ_BLOCK = 64 * 1024
SNIFF_BYTES = 4096
_PARTIAL = ".partial"
_STATUSES: tuple[str, ...] = get_args(ReportStatus)
_USER_DIR_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_UNSAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_SHA_RE = re.compile(r"[0-9a-fA-F]{64}")

# Canonical extensions per sniffed MIME type. A file whose name carries none of these gets the
# first one appended (or substituted for a known media extension); the sniffed type always wins.
_EXTS: dict[str, tuple[str, ...]] = {
    "image/png": (".png",),
    "image/jpeg": (".jpg", ".jpeg"),
    "image/gif": (".gif",),
    "image/webp": (".webp",),
    "image/bmp": (".bmp",),
    "image/avif": (".avif",),
    "image/heic": (".heic", ".heif"),
    "audio/mpeg": (".mp3",),
    "audio/aac": (".aac",),
    "audio/ogg": (".ogg", ".oga", ".opus"),
    "audio/wav": (".wav",),
    "audio/flac": (".flac",),
    "audio/mp4": (".m4a",),
    "video/mp4": (".mp4", ".m4v"),
    "video/quicktime": (".mov",),
    "video/webm": (".webm",),
    "video/x-matroska": (".mkv",),
    "video/x-msvideo": (".avi",),
    "application/pdf": (".pdf",),
    "application/zip": (".zip",),
    "application/gzip": (".gz", ".tgz"),
}
_KEEP_NAME_MIMES = frozenset({"text/plain", "application/octet-stream"})
_KNOWN_EXTS = frozenset(e for exts in _EXTS.values() for e in exts) | {".txt", ".md", ".bin"}
_FTYP_BRANDS: dict[bytes, tuple[str, FileKind]] = {
    b"M4A ": ("audio/mp4", "audio"),
    b"qt  ": ("video/quicktime", "video"),
    b"avif": ("image/avif", "image"),
    b"avis": ("image/avif", "image"),
    b"heic": ("image/heic", "image"),
    b"heix": ("image/heic", "image"),
    b"mif1": ("image/heic", "image"),
}


def sniff_mime(head: bytes) -> tuple[str, FileKind]:
    """Classify a file from its leading bytes. The file name is never trusted."""
    found = _sniff_binary(head)
    if found is not None:
        return found
    if head and b"\x00" not in head and _is_utf8_prefix(head):
        return "text/plain", "file"
    return "application/octet-stream", "file"


def _sniff_binary(head: bytes) -> tuple[str, FileKind] | None:
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", "image"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", "image"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif", "image"
    if len(head) >= 14 and head[:2] == b"BM" and head[6:10] == b"\x00\x00\x00\x00":
        return "image/bmp", "image"
    if head[:4] == b"RIFF" and len(head) >= 12:
        form = head[8:12]
        if form == b"WEBP":
            return "image/webp", "image"
        if form == b"WAVE":
            return "audio/wav", "audio"
        if form == b"AVI ":
            return "video/x-msvideo", "video"
    if head.startswith(b"OggS"):
        return "audio/ogg", "audio"
    if head.startswith(b"fLaC"):
        return "audio/flac", "audio"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        if b"webm" in head[:64]:
            return "video/webm", "video"
        return "video/x-matroska", "video"
    if len(head) >= 2 and head[0] == 0xFF:
        if head[1] & 0xF6 == 0xF0:  # ADTS AAC frame sync
            return "audio/aac", "audio"
        if head[1] & 0xE0 == 0xE0:  # MPEG audio frame sync
            return "audio/mpeg", "audio"
    if head.startswith(b"ID3"):
        return "audio/mpeg", "audio"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return _FTYP_BRANDS.get(head[8:12], ("video/mp4", "video"))
    if head.startswith(b"%PDF-"):
        return "application/pdf", "file"
    if head.startswith(b"PK\x03\x04"):
        return "application/zip", "file"
    if head.startswith(b"\x1f\x8b"):
        return "application/gzip", "file"
    return None


def _is_utf8_prefix(head: bytes) -> bool:
    try:
        head.decode("utf-8")
    except UnicodeDecodeError as err:
        # A sniff window may cut a multi-byte character at its end; that is still text.
        return err.reason == "unexpected end of data" and err.start >= len(head) - 3
    return True


def _final_name(name: str, mime: str) -> str:
    if mime in _KEEP_NAME_MIMES:
        return name
    exts = _EXTS[mime]
    suffix = os.path.splitext(name)[1].lower()
    if suffix in exts:
        return name
    stem = name[: -len(suffix)] if suffix in _KNOWN_EXTS else name
    return (stem.rstrip(".") or "file") + exts[0]


def _safe_name(raw: str) -> str:
    base = raw.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = _UNSAFE_NAME_RE.sub("_", base).strip("._")[:UPLOAD_NAME_MAX_CHARS]
    return cleaned or "file"


def _user_dir(user_id: str) -> str:
    if _USER_DIR_RE.fullmatch(user_id) and user_id.strip("."):
        return user_id
    return hashlib.sha256(user_id.encode()).hexdigest()[:32]


def _create_partial(directory: Path) -> None:
    directory.mkdir(parents=True)
    (directory / _PARTIAL).touch()


def _pwrite(path: Path, offset: int, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY)
    try:
        view = memoryview(data)
        done = 0
        while done < len(view):
            done += os.pwrite(fd, view[done:], offset + done)
    finally:
        os.close(fd)


def _truncate_quiet(path: Path, size: int) -> None:
    with contextlib.suppress(OSError):
        os.truncate(path, size)


def _read_head(path: Path) -> bytes:
    with path.open("rb") as fh:
        return fh.read(SNIFF_BYTES)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(READ_BLOCK), b""):
            digest.update(block)
    return digest.hexdigest()


def _sweep_orphans(root: Path, live: set[str], now: float, ttl: float) -> None:
    """Remove upload directories unknown to the registry and idle for longer than the TTL."""
    if not root.is_dir():
        return
    cutoff = now - ttl
    with os.scandir(root) as users:
        user_dirs = [e.path for e in users if e.is_dir(follow_symlinks=False)]
    for user_dir in user_dirs:
        with os.scandir(user_dir) as uploads:
            for entry in uploads:
                if entry.name in live or not entry.is_dir(follow_symlinks=False):
                    continue
                if entry.stat(follow_symlinks=False).st_mtime <= cutoff:
                    shutil.rmtree(entry.path, ignore_errors=True)


class _BridgeError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass(slots=True, eq=False)
class _Upload:
    upload_id: str
    user_id: str
    name: str
    declared_size: int
    dir: Path
    expires_at: float
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    next_index: int = 0
    received: int = 0
    closed: bool = False  # a short chunk arrived: the upload has ended
    dropped: bool = False
    path: Path | None = None  # set once completed
    token: str | None = None
    mime: str = ""
    kind: FileKind = "file"

    @property
    def partial(self) -> Path:
        return self.dir / _PARTIAL


_Handler = Callable[["BridgeHandle", web.Request], Awaitable[web.StreamResponse]]


def _guarded(handler: _Handler) -> _Handler:
    @functools.wraps(handler)
    async def wrapper(self: BridgeHandle, request: web.Request) -> web.StreamResponse:
        try:
            return await handler(self, request)
        except _BridgeError as err:
            return web.json_response({"error": err.code, "message": err.message}, status=err.status)

    return wrapper


def _int_field(body: dict[str, object], key: str) -> int:
    value = body.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _BridgeError(400, "bad_request", f"{key} must be a non-negative integer")
    return value


def _sha_field(body: dict[str, object]) -> str | None:
    value = body.get("sha256")
    if value is None:
        return None
    if not isinstance(value, str) or not _SHA_RE.fullmatch(value):
        raise _BridgeError(400, "bad_request", "sha256 must be 64 hex characters")
    return value.lower()


def _parse_index(raw: str) -> int:
    if not (raw.isascii() and raw.isdigit() and len(raw) <= 9):
        raise _BridgeError(400, "bad_request", "chunk index must be a non-negative integer")
    return int(raw)


async def _json_body(request: web.Request, limit: int) -> dict[str, object]:
    if request.content_length is not None and request.content_length > limit:
        raise _BridgeError(413, "body_too_large", f"body exceeds {limit} bytes")
    buf = bytearray()
    async for block in request.content.iter_chunked(8192):
        buf.extend(block)
        if len(buf) > limit:
            raise _BridgeError(413, "body_too_large", f"body exceeds {limit} bytes")
    try:
        body = json.loads(buf)
    except ValueError:
        raise _BridgeError(400, "invalid_json", "body is not valid JSON") from None
    if not isinstance(body, dict):
        raise _BridgeError(400, "bad_request", "body must be a JSON object")
    return body


def _disposition(name: str) -> str:
    return f'attachment; filename="{name}"'


class BridgeHandle:
    """Owns the bridge's upload/file registry and its TTL sweeper.

    Mount ``routes`` on the app, call ``start()`` once the loop runs and ``close()`` on shutdown.
    """

    def __init__(
        self,
        config: CodingConfig,
        auth: SandboxAuth,
        notifier: CodingNotifier,
        clock: Callable[[], float],
    ) -> None:
        self._temp = config.bridge.temp_dir
        self._max_bytes = config.bridge.max_file_bytes
        self._chunk = config.bridge.chunk_bytes
        self._ttl = config.bridge.file_ttl_seconds
        self._public = config.http.public_base_url.rstrip("/")
        self._auth = auth
        self._notifier = notifier
        self._clock = clock
        self._uploads: dict[str, _Upload] = {}
        self._tokens: dict[str, _Upload] = {}
        self._sweeper: asyncio.Task[None] | None = None
        self.routes: list[web.RouteDef] = [
            web.post("/bridge/uploads", self._create_upload),
            web.put("/bridge/uploads/{upload_id}/chunks/{index}", self._put_chunk),
            web.post("/bridge/uploads/{upload_id}/complete", self._complete_upload),
            web.post("/bridge/report", self._report),
            web.get("/bridge/files/{token}/{name}", self._download),
        ]

    async def start(self) -> None:
        if self._sweeper is None:
            self._sweeper = asyncio.create_task(self._run_sweeper(), name="bridge-sweeper")

    async def close(self) -> None:
        task, self._sweeper = self._sweeper, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def sweep(self) -> None:
        """Drop expired uploads and files together with their bytes; remove orphan directories."""
        now = self._clock()
        for up in [u for u in self._uploads.values() if u.expires_at <= now]:
            async with up.lock:
                if up.dropped or up.expires_at > self._clock():
                    continue
                self._forget(up)
                await asyncio.to_thread(shutil.rmtree, up.dir, ignore_errors=True)
        await asyncio.to_thread(
            _sweep_orphans, self._temp, set(self._uploads), self._clock(), self._ttl
        )

    async def _run_sweeper(self) -> None:
        interval = max(1.0, min(60.0, self._ttl / 10))
        while True:
            await asyncio.sleep(interval)
            try:
                await self.sweep()
            except Exception:
                log.exception("bridge sweep failed")

    # ------------------------------------------------------------------ helpers

    def _user(self, request: web.Request) -> str:
        scheme, _, token = request.headers.get("Authorization", "").partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token:
            raise _BridgeError(401, "unauthorized", "missing bearer token")
        user = self._auth.resolve(token)
        if user is None:
            raise _BridgeError(401, "unauthorized", "invalid bearer token")
        return user

    def _lookup(self, upload_id: str, user: str) -> _Upload:
        up = self._uploads.get(upload_id)
        if up is None or up.dropped or up.expires_at <= self._clock():
            raise _BridgeError(404, "unknown_upload", "upload not found or expired")
        if up.user_id != user:
            raise _BridgeError(403, "forbidden", "upload belongs to another user")
        return up

    def _check_live(self, up: _Upload) -> None:
        if up.dropped or up.expires_at <= self._clock():
            raise _BridgeError(404, "unknown_upload", "upload not found or expired")

    def _forget(self, up: _Upload) -> None:
        up.dropped = True
        self._uploads.pop(up.upload_id, None)
        if up.token is not None:
            self._tokens.pop(up.token, None)

    def _rollback(self, up: _Upload, offset: int, index: int) -> None:
        """Undo a failed chunk: the client must re-send ``index``."""
        _truncate_quiet(up.partial, offset)
        up.received = offset
        up.next_index = index
        up.closed = False

    def _stored(self, up: _Upload) -> StoredFile:
        if up.path is None or up.token is None:
            raise _BridgeError(409, "file_not_ready", "upload is not completed")
        return StoredFile(
            path=up.path,
            name=up.name,
            mime=up.mime,
            size=up.declared_size,
            kind=up.kind,
            url=f"{self._public}/bridge/files/{up.token}/{up.name}",
        )

    async def _stream_chunk(self, request: web.Request, up: _Upload, offset: int) -> int:
        await asyncio.to_thread(_truncate_quiet, up.partial, offset)
        written = 0
        async for block in request.content.iter_chunked(READ_BLOCK):
            end = offset + written + len(block)
            if end > self._max_bytes:
                raise _BridgeError(413, "file_too_large", f"file exceeds {self._max_bytes} bytes")
            if end > up.declared_size:
                raise _BridgeError(400, "size_mismatch", "chunk data exceeds the declared size")
            await asyncio.to_thread(_pwrite, up.partial, offset + written, block)
            written += len(block)
        return written

    async def _finish(self, up: _Upload, size: int, sha256: str | None) -> None:
        if size != up.declared_size:
            raise _BridgeError(400, "size_mismatch", "size differs from the declared size")
        if up.received != up.declared_size:
            raise _BridgeError(
                409, "incomplete", f"received {up.received} of {up.declared_size} bytes"
            )
        if sha256 is not None:
            actual = await asyncio.to_thread(_sha256_file, up.partial)
            if actual != sha256:
                raise _BridgeError(400, "sha256_mismatch", "sha256 does not match the data")
        mime, kind = sniff_mime(await asyncio.to_thread(_read_head, up.partial))
        name = _final_name(up.name, mime)
        path = up.dir / name
        await asyncio.to_thread(os.replace, up.partial, path)
        token = secrets.token_urlsafe(24)
        up.path, up.name, up.mime, up.kind = path, name, mime, kind
        up.token = token
        self._tokens[token] = up
        up.expires_at = self._clock() + self._ttl

    @staticmethod
    def _complete_payload(up: _Upload) -> dict[str, object]:
        return {
            "upload_id": up.upload_id,
            "name": up.name,
            "mime": up.mime,
            "size": up.declared_size,
            "kind": up.kind,
        }

    # ------------------------------------------------------------------ handlers

    @_guarded
    async def _create_upload(self, request: web.Request) -> web.StreamResponse:
        user = self._user(request)
        body = await _json_body(request, SMALL_JSON_LIMIT)
        raw_name = body.get("name")
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise _BridgeError(400, "bad_request", "name must be a non-empty string")
        size = _int_field(body, "size")
        if size > self._max_bytes:
            raise _BridgeError(413, "file_too_large", f"file exceeds {self._max_bytes} bytes")
        upload_id = uuid.uuid4().hex
        up = _Upload(
            upload_id=upload_id,
            user_id=user,
            name=_safe_name(raw_name),
            declared_size=size,
            dir=self._temp / _user_dir(user) / upload_id,
            expires_at=self._clock() + self._ttl,
        )
        self._uploads[upload_id] = up
        try:
            await asyncio.to_thread(_create_partial, up.dir)
        except OSError:
            self._forget(up)
            raise _BridgeError(500, "storage_error", "cannot create upload storage") from None
        return web.json_response({"upload_id": upload_id}, status=201)

    @_guarded
    async def _put_chunk(self, request: web.Request) -> web.StreamResponse:
        user = self._user(request)
        up = self._lookup(request.match_info["upload_id"], user)
        index = _parse_index(request.match_info["index"])
        length = request.content_length
        if length is None:
            raise _BridgeError(411, "length_required", "Content-Length is required")
        if length == 0:
            raise _BridgeError(400, "empty_chunk", "chunk body is empty")
        if length > self._chunk:
            raise _BridgeError(413, "chunk_too_large", f"chunk exceeds {self._chunk} bytes")
        async with up.lock:
            self._check_live(up)
            if index == up.next_index and up.closed:
                raise _BridgeError(409, "upload_closed", "upload already ended")
            if index != up.next_index and index != up.next_index - 1:
                raise _BridgeError(409, "out_of_order", f"expected chunk index {up.next_index}")
            offset = index * self._chunk
            try:
                written = await self._stream_chunk(request, up, offset)
            except _BridgeError as err:
                if err.code == "file_too_large":
                    self._forget(up)
                    await asyncio.to_thread(shutil.rmtree, up.dir, ignore_errors=True)
                else:
                    self._rollback(up, offset, index)
                raise
            except BaseException:
                self._rollback(up, offset, index)
                raise
            up.received = offset + written
            up.next_index = index + 1
            up.closed = written < self._chunk
            up.expires_at = self._clock() + self._ttl
        return web.json_response({"index": index, "received": up.received})

    @_guarded
    async def _complete_upload(self, request: web.Request) -> web.StreamResponse:
        user = self._user(request)
        up = self._lookup(request.match_info["upload_id"], user)
        body = await _json_body(request, SMALL_JSON_LIMIT)
        size = _int_field(body, "size")
        sha256 = _sha_field(body)
        async with up.lock:
            self._check_live(up)
            if up.path is None:
                await self._finish(up, size, sha256)
            return web.json_response(self._complete_payload(up))

    @_guarded
    async def _report(self, request: web.Request) -> web.StreamResponse:
        user = self._user(request)
        body = await _json_body(request, REPORT_MAX_BODY)
        message = body.get("message")
        if not isinstance(message, str):
            raise _BridgeError(400, "bad_request", "message must be a string")
        if len(message) > REPORT_MAX_CHARS:
            raise _BridgeError(400, "message_too_long", f"message exceeds {REPORT_MAX_CHARS} chars")
        status = body.get("status", "running")
        if status not in _STATUSES:
            raise _BridgeError(400, "bad_request", f"status must be one of {_STATUSES}")
        thread_id = body.get("thread_id")
        if thread_id is not None and (
            not isinstance(thread_id, str) or not thread_id or len(thread_id) > THREAD_ID_MAX_CHARS
        ):
            raise _BridgeError(400, "bad_request", "thread_id must be a non-empty string")
        file_ids = body.get("files", [])
        if not isinstance(file_ids, list) or not all(isinstance(f, str) for f in file_ids):
            raise _BridgeError(400, "bad_request", "files must be a list of upload ids")
        if len(file_ids) > REPORT_MAX_FILES:
            raise _BridgeError(400, "too_many_files", f"at most {REPORT_MAX_FILES} files")
        if len(set(file_ids)) != len(file_ids):
            raise _BridgeError(400, "duplicate_file", "files must be unique")
        files = tuple(self._stored(self._lookup(fid, user)) for fid in file_ids)
        notice = CodingNotice(
            kind="report",
            user_id=user,
            thread_id=thread_id,
            title=None,
            text=message,
            status=cast(ReportStatus, status),
            files=files,
        )
        try:
            await self._notifier.notify(notice)
        except Exception as exc:
            # Never log the message body or the exception text (it may echo the payload).
            log.warning("bridge report delivery failed: %s", type(exc).__name__)
            raise _BridgeError(503, "notify_failed", "report could not be delivered") from None
        return web.json_response({"ok": True})

    @_guarded
    async def _download(self, request: web.Request) -> web.StreamResponse:
        up = self._tokens.get(request.match_info["token"])
        name = request.match_info["name"]
        if (
            up is None
            or up.dropped
            or up.path is None
            or up.name != name
            or up.expires_at <= self._clock()
        ):
            raise _BridgeError(404, "not_found", "file not found or expired")
        return web.FileResponse(
            up.path,
            headers={
                "Content-Type": up.mime,
                "Content-Disposition": _disposition(up.name),
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
            },
        )


def bridge_routes(
    config: CodingConfig,
    auth: SandboxAuth,
    notifier: CodingNotifier,
    *,
    clock: Callable[[], float] = time.time,
) -> BridgeHandle:
    """Build the ``/bridge`` routes and their registry; see ``BridgeHandle``."""
    return BridgeHandle(config, auth, notifier, clock)
