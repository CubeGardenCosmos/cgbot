"""Image generation for coding sandboxes (M5, slice F).

Ported from the legacy manager's ``imagegen.py``: the provider call now runs on
``httpx.AsyncClient``
inside the bot process. Credentials stay in the bot; the sandbox only ever sends a prompt, a size,
a name and sandbox-side reference paths. The user identity comes from the bridge bearer token.

All host-side file access walks from ``/`` with ``O_NOFOLLOW`` at every component, so a symlink
inside a user workspace can never redirect a read or write outside it.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hashlib
import io
import json
import logging
import os
import re
import secrets
import stat
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiohttp.web
import httpx
from PIL import Image

from cgbot.coding.config import CodingConfig, ImageGenConfig
from cgbot.coding.contracts import SandboxAuth

log = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_DIMENSION = 12000
MAX_CONTENT_WIDTH = 1536
MAX_CONTENT_HEIGHT = 1024
MAX_EDGE = 3840
MAX_ASPECT = 3
MAX_REFERENCES = 8
MAX_PROMPT_CHARS = 8000
MAX_REFERENCE_PATH_CHARS = 512
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_ERROR_BODY_BYTES = 4096
DEFAULT_SIZE = "1024x1024"
UNKNOWN_STATUS = frozenset({408, 504, 524})
SANDBOX_WORKSPACE = "/workspace"
OUTPUT_PARTS = ("artifacts", "imagegen")
INDEX_NAME = "index.jsonl"
OWNER = 1000

_USER_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_NAME_RE = re.compile(r"[a-z0-9_-]{1,40}")
_SIZE_RE = re.compile(r"(\d{1,5})x(\d{1,5})")
_MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\((https?://[^\s)]+|data:image[^)\s]+)\)")
_URL_RE = re.compile(r"https?://[^\s)\"'<>]+")

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_WRITE_NEW_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
_APPEND_FLAGS = (
    os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
)


class ImageGenError(Exception):
    """Public, typed failure. ``status`` and ``code`` are what the bridge answers with."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class _GenerationFailed(Exception):
    """The provider definitely did not generate an image (request not sent or rejected)."""


class _OutcomeUnknown(Exception):
    """The provider may have processed and billed the request; never resend automatically."""


class _InvalidImage(ValueError):
    """Bytes are not a usable image (user reference or provider output)."""


@dataclass(frozen=True, slots=True)
class ImageResult:
    """A stored image. ``path`` is the sandbox path under ``/workspace``."""

    path: str
    width: int
    height: int
    bytes: int
    size: str


@dataclass(frozen=True, slots=True)
class ImageInfo:
    mime: str
    width: int
    height: int
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class _Reference:
    data: bytes
    info: ImageInfo


@dataclass(frozen=True, slots=True)
class _ProviderImage:
    data: bytes
    mime: str


@dataclass(frozen=True, slots=True)
class _Request:
    prompt: str
    size: str | None
    name: str | None
    references: tuple[str, ...]


# ---------------------------------------------------------------------------
# Pure helpers: size, image inspection, provider payloads
# ---------------------------------------------------------------------------


def parse_size(text: str) -> str:
    """Validate an explicit ``WIDTHxHEIGHT`` size. Each edge is a multiple of 16."""
    match = _SIZE_RE.fullmatch(text)
    if match is None:
        raise _InvalidImage("size must look like 1024x1024")
    width, height = int(match.group(1)), int(match.group(2))
    if width % 16 or height % 16 or not 16 <= width <= MAX_EDGE or not 16 <= height <= MAX_EDGE:
        raise _InvalidImage(f"size edges must be multiples of 16 between 16 and {MAX_EDGE}")
    if max(width, height) > MAX_ASPECT * min(width, height):
        raise _InvalidImage(f"size aspect ratio must be at most {MAX_ASPECT}:1")
    return f"{width}x{height}"


def resolve_size(reference: tuple[int, int] | None) -> str:
    """Pick the output size: the reference's shape within the content box, else the default."""
    if reference is None:
        return DEFAULT_SIZE
    width, height = reference
    long_px, short_px = max(width, height), min(width, height)
    scale = min(1.0, MAX_CONTENT_WIDTH / long_px, MAX_CONTENT_HEIGHT / short_px)
    long_px = round(long_px * scale)
    short_px = round(short_px * scale)
    # Snap to the 16px grid; the short edge may need to grow to respect the aspect limit.
    long_px = max(16, round(long_px / 16) * 16)
    short_px = max(16, round(short_px / 16) * 16)
    minimum_short = -(-long_px // MAX_ASPECT)
    short_px = max(short_px, -(-minimum_short // 16) * 16)
    if width >= height:
        return f"{long_px}x{short_px}"
    return f"{short_px}x{long_px}"


def sniff_mime(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def inspect_image(data: bytes) -> ImageInfo:
    """Validate a user-supplied reference image (PNG, JPEG or WebP within the size limits)."""
    if len(data) > MAX_IMAGE_BYTES:
        raise _InvalidImage("image exceeds 20 MiB")
    mime = sniff_mime(data)
    if mime is None:
        raise _InvalidImage("only PNG, JPEG and WebP images are accepted")
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            fmt = image.format
    except Exception:
        raise _InvalidImage("image data cannot be decoded") from None
    expected = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP"}[mime]
    if fmt != expected:
        raise _InvalidImage("image content does not match its format")
    if not (0 < width <= MAX_DIMENSION and 0 < height <= MAX_DIMENSION):
        raise _InvalidImage(f"image dimensions must be at most {MAX_DIMENSION}px per side")
    return ImageInfo(
        mime=mime,
        width=width,
        height=height,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )


def normalize_png(payload: bytes) -> tuple[bytes, int, int]:
    """Decode provider output and return PNG bytes plus dimensions. Non-PNG is re-encoded."""
    try:
        with Image.open(io.BytesIO(payload)) as image:
            image.load()
            width, height = image.size
            if image.format == "PNG":
                return payload, width, height
            target = image.convert("RGBA" if "A" in image.getbands() else "RGB")
            buffer = io.BytesIO()
            target.save(buffer, "PNG")
    except Exception:
        raise _GenerationFailed(
            "image provider returned data that is not a valid image (the request may have been "
            "billed)"
        ) from None
    return buffer.getvalue(), width, height


def data_url(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _decode_data_url(value: str) -> bytes:
    header, sep, encoded = value.partition(",")
    if not sep or ";base64" not in header:
        raise _GenerationFailed("image provider returned an unsupported data URL")
    try:
        return base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise _GenerationFailed("image provider returned invalid base64 image data") from None


def extract_payload(result: object, images_mode: bool) -> bytes | str:
    """Return image bytes, or an image URL that still needs downloading."""
    if not isinstance(result, dict):
        raise _GenerationFailed("image provider response is not a JSON object")
    candidates: list[object] = []
    if images_mode:
        data = result.get("data")
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            raise _GenerationFailed("image provider response has no image data")
        first = data[0]
        if first.get("b64_json"):
            try:
                return base64.b64decode(first["b64_json"], validate=True)
            except (ValueError, TypeError, binascii.Error):
                raise _GenerationFailed(
                    "image provider returned invalid base64 image data"
                ) from None
        candidates.append(first.get("url"))
    else:
        choices = result.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise _GenerationFailed("image provider response has no choices")
        message = choices[0].get("message")
        content: object = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            match = _MARKDOWN_IMAGE_RE.search(content)
            if match is not None:
                candidates.append(match.group(1))
            else:
                url = _URL_RE.search(content)
                candidates.append(url.group(0) if url is not None else None)
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "image_url" and isinstance(part.get("image_url"), dict):
                    candidates.append(part["image_url"].get("url"))
                elif part.get("type") == "image_url":
                    candidates.append(part.get("image_url"))
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.startswith("data:image"):
            return _decode_data_url(candidate)
        if isinstance(candidate, str) and candidate.startswith(("http://", "https://")):
            return candidate
    raise _GenerationFailed("image provider response contains no image")


def build_request(
    config: ImageGenConfig, prompt: str, images: Sequence[_ProviderImage], size: str
) -> tuple[str, dict[str, Any]]:
    """Return ``(path, body)`` for the configured provider mode."""
    urls = [data_url(image.data, image.mime) for image in images]
    if config.mode == "images":
        if urls:
            body: dict[str, Any] = {
                "model": config.model,
                "prompt": prompt,
                "size": size,
                "image": urls,
            }
            return "/images/edits", body
        return "/images/generations", {"model": config.model, "prompt": prompt, "size": size}
    content: list[dict[str, Any]] = [
        {"type": "text", "text": f"{prompt}\n\nOutput image size should be approximately {size}."}
    ]
    content.extend({"type": "image_url", "image_url": {"url": url}} for url in urls)
    chat: dict[str, Any] = {
        "model": config.model,
        "messages": [{"role": "user", "content": content}],
    }
    return "/chat/completions", chat


def default_name() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    return f"img{stamp}{secrets.token_hex(2)}"


def _sandbox_parts(path: str) -> tuple[str, ...]:
    """Split an absolute ``/workspace/...`` path into components, rejecting anything unsafe."""
    prefix = SANDBOX_WORKSPACE + "/"
    if not path.startswith(prefix):
        raise ImageGenError(
            400, "invalid_request", "reference image paths must be absolute /workspace paths"
        )
    parts = tuple(path[len(prefix) :].split("/"))
    if any(part in ("", ".", "..") or "\x00" in part or "\\" in part for part in parts):
        raise ImageGenError(400, "invalid_request", "reference image path is not allowed")
    return parts


# ---------------------------------------------------------------------------
# Host filesystem: O_NOFOLLOW walk under the user workspace
# ---------------------------------------------------------------------------


def _child_dir(parent: int, name: str, *, create: bool) -> int:
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=parent)
    except FileNotFoundError:
        if not create:
            raise
    with contextlib.suppress(FileExistsError):
        os.mkdir(name, 0o755, dir_fd=parent)
    os.chown(name, OWNER, OWNER, dir_fd=parent, follow_symlinks=False)
    return os.open(name, _DIR_FLAGS, dir_fd=parent)


def _open_workspace(host_root: Path, user_id: str, parts: Sequence[str], *, create: bool) -> int:
    """Open ``<host_root>/users/<user_id>/<parts...>`` as a directory fd, no symlink hops."""
    if not host_root.is_absolute() or any(p in (".", "..") for p in host_root.parts):
        raise OSError("host data root must be an absolute, normalised path")
    steps: list[tuple[str, bool]] = [(part, False) for part in host_root.parts[1:]]
    steps.append(("users", create))
    steps.append((user_id, create))
    steps.extend((part, create) for part in parts)
    fd = os.open("/", _DIR_FLAGS)
    try:
        for name, make in steps:
            child = _child_dir(fd, name, create=make)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


class WorkspaceFileTooLarge(OSError):
    """A workspace file is larger than the caller's byte limit."""


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _read_all_bounded(fd: int, limit: int) -> bytes:
    data = bytearray()
    while len(data) <= limit:
        chunk = os.read(fd, min(65536, limit + 1 - len(data)))
        if not chunk:
            break
        data.extend(chunk)
    if len(data) > limit:
        raise WorkspaceFileTooLarge(f"file exceeds {limit} bytes")
    return bytes(data)


def read_workspace_file(
    host_root: Path, user_id: str, parts: Sequence[str], max_bytes: int
) -> bytes:
    """Read ``<host_root>/users/<user_id>/<parts...>`` with no symlink hops, at most ``max_bytes``.

    Raises ``WorkspaceFileTooLarge`` above the limit; any other failure (not a stable regular
    single-link file, unsafe path, changed while reading) is an ``OSError``.
    """
    if not _USER_ID_RE.fullmatch(user_id) or not parts:
        raise OSError("invalid workspace path")
    if any(part in ("", ".", "..") or "\x00" in part or "\\" in part for part in parts):
        raise OSError("unsafe workspace path")
    dir_fd = _open_workspace(host_root, user_id, parts[:-1], create=False)
    try:
        fd = os.open(parts[-1], _FILE_FLAGS, dir_fd=dir_fd)
    finally:
        os.close(dir_fd)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise OSError("not a regular single-link file")
        if before.st_size > max_bytes:
            raise WorkspaceFileTooLarge(f"file exceeds {max_bytes} bytes")
        data = _read_all_bounded(fd, max_bytes)
        after = os.fstat(fd)
        if _identity(before) != _identity(after) or len(data) != before.st_size:
            raise OSError("file changed while reading")
        return data
    finally:
        os.close(fd)


def _read_reference(host_root: Path, user_id: str, parts: tuple[str, ...]) -> bytes:
    try:
        return read_workspace_file(host_root, user_id, parts, MAX_IMAGE_BYTES)
    except WorkspaceFileTooLarge:
        raise _InvalidImage("image exceeds 20 MiB") from None


def _load_references(host_root: Path, user_id: str, paths: Sequence[str]) -> list[_Reference]:
    references: list[_Reference] = []
    for path in paths:
        parts = _sandbox_parts(path)
        try:
            data = _read_reference(host_root, user_id, parts)
        except _InvalidImage as error:
            raise ImageGenError(
                400, "invalid_request", f"Reference image {path}: {error}"
            ) from None
        except OSError:
            raise ImageGenError(
                400, "invalid_request", f"Reference image {path} is not an accessible file"
            ) from None
        try:
            info = inspect_image(data)
        except _InvalidImage as error:
            raise ImageGenError(
                400, "invalid_request", f"Reference image {path}: {error}"
            ) from None
        references.append(_Reference(data=data, info=info))
    return references


def _check_name_free(host_root: Path, user_id: str, stem: str) -> None:
    dir_fd = _open_workspace(host_root, user_id, OUTPUT_PARTS, create=True)
    try:
        try:
            os.stat(f"{stem}.png", dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
    finally:
        os.close(dir_fd)
    raise ImageGenError(400, "invalid_request", "An image with this name already exists")


def _write_new(dir_fd: int, name: str, data: bytes) -> None:
    fd = os.open(name, _WRITE_NEW_FLAGS, 0o644, dir_fd=dir_fd)
    try:
        os.fchown(fd, OWNER, OWNER)
        os.fchmod(fd, 0o644)
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _append_line(dir_fd: int, name: str, line: bytes) -> None:
    fd = os.open(name, _APPEND_FLAGS, 0o644, dir_fd=dir_fd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("index is not a regular file")
        os.fchown(fd, OWNER, OWNER)
        os.fchmod(fd, 0o644)
        view = memoryview(line)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _store_output(host_root: Path, user_id: str, stem: str, png: bytes, record: bytes) -> None:
    dir_fd = _open_workspace(host_root, user_id, OUTPUT_PARTS, create=True)
    try:
        try:
            _write_new(dir_fd, f"{stem}.png", png)
        except FileExistsError:
            raise ImageGenError(
                400, "invalid_request", "An image with this name already exists"
            ) from None
        _append_line(dir_fd, INDEX_NAME, record)
    finally:
        os.close(dir_fd)


# ---------------------------------------------------------------------------
# Provider client
# ---------------------------------------------------------------------------


class _Provider:
    def __init__(self, config: ImageGenConfig, transport: httpx.AsyncBaseTransport | None) -> None:
        self._config = config
        # An injected transport owns all routing, so ambient proxy env vars must not bypass it.
        trust_env = transport is None
        timeout = httpx.Timeout(config.timeout_seconds)
        proxy = config.proxy or None
        self._api = httpx.AsyncClient(
            proxy=proxy,
            transport=transport,
            timeout=timeout,
            follow_redirects=False,
            trust_env=trust_env,
        )
        self._fetch = httpx.AsyncClient(
            proxy=proxy,
            transport=transport,
            timeout=timeout,
            follow_redirects=True,
            trust_env=trust_env,
        )

    @property
    def model(self) -> str:
        return self._config.model

    async def close(self) -> None:
        await self._api.aclose()
        await self._fetch.aclose()

    async def _post(self, url: str, body: bytes) -> tuple[int, bytes]:
        headers = {
            "Authorization": f"Bearer {self._config.api_key.reveal()}",
            "Content-Type": "application/json",
        }
        try:
            async with self._api.stream("POST", url, headers=headers, content=body) as response:
                status = response.status_code
                success = 200 <= status < 300
                limit = MAX_RESPONSE_BYTES if success else MAX_ERROR_BODY_BYTES
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > limit:
                        if success:
                            raise _OutcomeUnknown(
                                "image provider response is too large; it may have been billed"
                            )
                        break
                return status, bytes(data)
        except (
            httpx.UnsupportedProtocol,
            httpx.InvalidURL,
            httpx.ConnectError,
            httpx.ProxyError,
        ):
            raise _GenerationFailed(
                "could not connect to the image provider; the request was not sent"
            ) from None
        except httpx.TimeoutException:
            raise _OutcomeUnknown(
                "image provider timed out; it may have processed and billed the request"
            ) from None
        except (httpx.HTTPError, OSError):
            raise _OutcomeUnknown(
                "connection to the image provider was lost; it may have processed and billed "
                "the request"
            ) from None

    async def _download(self, url: str, started: float) -> bytes:
        remaining = max(1.0, self._config.timeout_seconds - (time.monotonic() - started))
        try:
            async with self._fetch.stream("GET", url, timeout=remaining) as response:
                if not 200 <= response.status_code < 300:
                    raise _OutcomeUnknown(
                        "generated image URL could not be downloaded; do not regenerate"
                    )
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_IMAGE_BYTES:
                        raise _OutcomeUnknown(
                            "generated image is too large to store; do not regenerate"
                        )
                return bytes(data)
        except (httpx.HTTPError, OSError):
            raise _OutcomeUnknown(
                "generated image URL could not be downloaded; do not regenerate"
            ) from None

    async def generate(self, prompt: str, images: Sequence[_ProviderImage], size: str) -> bytes:
        path, body = build_request(self._config, prompt, images, size)
        encoded = json.dumps(body).encode("utf-8")
        started = time.monotonic()
        log.info(
            "image generation request model=%s size=%s references=%d",
            self._config.model,
            size,
            len(images),
        )
        status, data = await self._post(self._config.base_url.rstrip("/") + path, encoded)
        if status in UNKNOWN_STATUS:
            raise _OutcomeUnknown(
                f"image provider returned HTTP {status}; it may have processed and billed the "
                "request"
            )
        if not 200 <= status < 300:
            raise _GenerationFailed(f"image provider rejected the request: HTTP {status}")
        try:
            result = json.loads(data)
        except ValueError:
            raise _GenerationFailed(
                "image provider returned invalid JSON (the request may have been billed)"
            ) from None
        payload = extract_payload(result, images_mode=self._config.mode == "images")
        if isinstance(payload, str):
            return await self._download(payload, started)
        return payload


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


def _validate(
    prompt: object,
    size: object,
    name: object,
    references: object,
) -> _Request:
    if not isinstance(prompt, str) or not prompt.strip():
        raise ImageGenError(400, "invalid_request", "prompt must be a non-blank string")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ImageGenError(400, "invalid_request", "prompt must be at most 8000 characters")
    parsed_size: str | None = None
    if size is not None:
        if not isinstance(size, str):
            raise ImageGenError(400, "invalid_request", "size must be a string")
        try:
            parsed_size = parse_size(size)
        except _InvalidImage as error:
            raise ImageGenError(400, "invalid_request", str(error)) from None
    parsed_name: str | None = None
    if name is not None:
        if not isinstance(name, str) or _NAME_RE.fullmatch(name) is None:
            raise ImageGenError(
                400, "invalid_request", "name must be 1-40 characters of a-z, 0-9, _ or -"
            )
        parsed_name = name
    if references is None:
        references = ()
    if not isinstance(references, (list, tuple)):
        raise ImageGenError(400, "invalid_request", "reference_images must be a list")
    if len(references) > MAX_REFERENCES:
        raise ImageGenError(400, "invalid_request", "at most 8 reference images are allowed")
    paths: list[str] = []
    for path in references:
        if not isinstance(path, str) or not 1 <= len(path) <= MAX_REFERENCE_PATH_CHARS:
            raise ImageGenError(400, "invalid_request", "reference image paths must be strings")
        _sandbox_parts(path)
        paths.append(path)
    return _Request(
        prompt=prompt,
        size=parsed_size,
        name=parsed_name,
        references=tuple(paths),
    )


class ImageGenService:
    """Generate one image per user at a time through the configured provider."""

    def __init__(
        self,
        config: CodingConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._host_root = config.docker.host_data_dir
        self._imagegen = config.imagegen
        self._provider = (
            _Provider(config.imagegen, transport) if config.imagegen is not None else None
        )
        self._busy: set[str] = set()

    async def close(self) -> None:
        if self._provider is not None:
            await self._provider.close()

    async def generate(
        self,
        user_id: str,
        prompt: str,
        *,
        size: str | None = None,
        name: str | None = None,
        reference_images: Sequence[str] | None = None,
    ) -> ImageResult:
        if self._provider is None or self._imagegen is None:
            raise ImageGenError(503, "not_configured", "Image generation is not configured")
        if not isinstance(user_id, str) or _USER_ID_RE.fullmatch(user_id) is None:
            raise ImageGenError(400, "invalid_request", "invalid user id")
        request = _validate(prompt, size, name, reference_images)
        # Check-and-add has no await in between, so it is atomic on the event loop.
        if user_id in self._busy:
            raise ImageGenError(409, "busy", "An image is already being generated for this user")
        if len(self._busy) >= self._imagegen.max_concurrency:
            raise ImageGenError(429, "too_many", "Too many image generations are running")
        self._busy.add(user_id)
        try:
            return await self._generate(self._provider, user_id, request)
        finally:
            self._busy.discard(user_id)

    async def _generate(self, provider: _Provider, user_id: str, request: _Request) -> ImageResult:
        references = await asyncio.to_thread(
            _load_references, self._host_root, user_id, request.references
        )
        size = request.size
        if size is None:
            first = references[0].info if references else None
            size = resolve_size((first.width, first.height) if first is not None else None)
        stem = request.name or default_name()
        await asyncio.to_thread(_check_name_free, self._host_root, user_id, stem)

        started = time.monotonic()
        images = [_ProviderImage(data=ref.data, mime=ref.info.mime) for ref in references]
        try:
            raw = await provider.generate(request.prompt, images, size)
            png, width, height = await asyncio.to_thread(normalize_png, raw)
        except _GenerationFailed as error:
            raise ImageGenError(502, "generation_failed", str(error)) from None
        except _OutcomeUnknown as error:
            raise ImageGenError(
                502, "outcome_unknown", f"{error}; do not retry automatically"
            ) from None
        log.info(
            "image generated user=%s size=%s references=%d elapsed=%.1fs",
            user_id,
            size,
            len(references),
            time.monotonic() - started,
        )

        filename = f"{stem}.png"
        sandbox_path = f"{SANDBOX_WORKSPACE}/{'/'.join(OUTPUT_PARTS)}/{filename}"
        record = {
            "created_at": datetime.now(UTC).isoformat(),
            "path": sandbox_path,
            "sha256": hashlib.sha256(png).hexdigest(),
            "prompt": request.prompt,
            "reference_sha256": [ref.info.sha256 for ref in references],
            "model": provider.model,
            "size": size,
        }
        line = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            await asyncio.to_thread(_store_output, self._host_root, user_id, stem, png, line)
        except ImageGenError:
            raise
        except OSError:
            log.exception("Storing generated image failed")
            raise ImageGenError(
                500, "storage_error", "Generated image could not be saved"
            ) from None
        return ImageResult(
            path=sandbox_path,
            width=width,
            height=height,
            bytes=len(png),
            size=size,
        )


# ---------------------------------------------------------------------------
# Bridge route
# ---------------------------------------------------------------------------


def _error_response(status: int, code: str, message: str) -> aiohttp.web.Response:
    return aiohttp.web.json_response({"error": code, "message": message}, status=status)


def _bearer_user(request: aiohttp.web.Request, auth: SandboxAuth) -> str | None:
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return None
    return auth.resolve(token)


def imagegen_routes(service: ImageGenService, auth: SandboxAuth) -> list[aiohttp.web.RouteDef]:
    """Routes for the bridge: ``POST /bridge/imagegen``. The user id never comes from the body."""

    async def generate(request: aiohttp.web.Request) -> aiohttp.web.Response:
        user_id = _bearer_user(request, auth)
        if user_id is None:
            return _error_response(401, "unauthorized", "Unauthorized")
        try:
            body = await request.json()
        except ValueError:
            return _error_response(400, "invalid_request", "request body must be JSON")
        if not isinstance(body, dict):
            return _error_response(400, "invalid_request", "request body must be a JSON object")
        prompt = body.get("prompt")
        if not isinstance(prompt, str):
            return _error_response(400, "invalid_request", "prompt must be a string")
        try:
            result = await service.generate(
                user_id,
                prompt,
                size=body.get("size"),
                name=body.get("name"),
                reference_images=body.get("reference_images"),
            )
        except ImageGenError as error:
            return _error_response(error.status, error.code, error.message)
        return aiohttp.web.json_response(
            {
                "path": result.path,
                "width": result.width,
                "height": result.height,
                "bytes": result.bytes,
                "size": result.size,
            }
        )

    return [aiohttp.web.post("/bridge/imagegen", generate)]
