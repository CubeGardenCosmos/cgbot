"""Bounded, SSRF-safe inbound images stored in each sandbox's workspace inbox."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import ipaddress
import os
import re
import socket
import stat
import time
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult
from aiohttp.resolver import ThreadedResolver

from cgbot.coding.contracts import workspace_dir

MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_STAGED_FILES = 50
FETCH_TIMEOUT_SECONDS = 30
_USER_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_STAGED_NAME_RE = re.compile(r"\d{8}-\d{6}-[1-9]\d*\.(?:png|jpg|webp|gif)")
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_NEW_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC


class StageError(Exception):
    """Public failure with a stable code; messages never contain remote URLs."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _check_address(host: str) -> None:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            # libc accepts legacy numeric IPv4 spellings too (127.1, hex, integers).
            address = ipaddress.IPv4Address(socket.inet_aton(host))
        except OSError:
            return  # Domain names are checked by the resolver at connection time.
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if not address.is_global or address.is_multicast:
        raise StageError("blocked_address")


def _validate_url(url: str) -> None:
    if not url or any(ord(char) <= 32 or 127 <= ord(char) <= 159 for char in url) or "\\" in url:
        raise StageError("invalid_url")
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or "%" in host
            or (port is not None and port < 1)
        ):
            raise ValueError
        host.encode("idna")
    except (ValueError, UnicodeError):
        raise StageError("invalid_url") from None
    _check_address(host)


class _PublicResolver(AbstractResolver):
    """Return only checked numeric addresses, never re-resolving them in the connector."""

    def __init__(self) -> None:
        self._resolver = ThreadedResolver()

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        results = await self._resolver.resolve(host, port, family)
        for result in results:
            # The underlying resolver returns numeric hosts. Fail closed otherwise.
            try:
                ipaddress.ip_address(result["host"])
            except ValueError:
                raise StageError("blocked_address") from None
            _check_address(result["host"])
        if not results:
            raise StageError("download_failed")
        return results

    async def close(self) -> None:
        await self._resolver.close()


async def default_fetch(url: str) -> bytes:
    """Fetch without proxies, redirects or unchecked DNS, bounded after decompression."""
    _validate_url(url)
    resolver = _PublicResolver()
    try:
        connector = aiohttp.TCPConnector(resolver=resolver, use_dns_cache=False)
        timeout = aiohttp.ClientTimeout(total=30, connect=10, sock_read=10)
        async with aiohttp.ClientSession(
            connector=connector, timeout=timeout, trust_env=False
        ) as session:
            async with session.get(url, allow_redirects=False) as response:
                if response.status != 200:
                    raise StageError("download_failed")
                if (
                    response.content_length is not None
                    and response.content_length > MAX_IMAGE_BYTES
                ):
                    raise StageError("too_large")
                data = bytearray()
                async for chunk in response.content.iter_chunked(64 * 1024):
                    if len(data) + len(chunk) > MAX_IMAGE_BYTES:
                        raise StageError("too_large")
                    data.extend(chunk)
                return bytes(data)
    except StageError:
        raise
    except Exception:
        raise StageError("download_failed") from None
    finally:
        await resolver.close()


def _extension(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    raise StageError("unsupported_image")


@contextlib.contextmanager
def _inbox_fd(workspace: Path) -> Iterator[int]:
    # Walk from the filesystem root without resolve(): resolving first would trust symlinks.
    path = workspace.absolute() / "inbox"
    if ".." in path.parts:
        raise StageError("unsafe_path")
    fd = os.open(path.anchor, _DIR_FLAGS)
    try:
        for component in path.parts[1:]:
            created = False
            try:
                os.mkdir(component, mode=0o755, dir_fd=fd)
                created = True
            except FileExistsError:
                pass
            next_fd = os.open(component, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = next_fd
            if created:
                os.fchmod(fd, 0o755)
        yield fd
    finally:
        os.close(fd)


def _prune(fd: int) -> None:
    files: list[tuple[int, str]] = []
    for name in os.listdir(fd):
        if not _STAGED_NAME_RE.fullmatch(name):
            continue
        try:
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if stat.S_ISREG(info.st_mode):
            files.append((info.st_mtime_ns, name))
    files.sort()
    for _, name in files[: max(0, len(files) - MAX_STAGED_FILES)]:
        # unlink is relative to the pinned inbox and never follows symlinks.
        try:
            os.unlink(name, dir_fd=fd)
        except FileNotFoundError:
            pass


class ImageStager:
    """Stage magic-sniffed images; fetch and clock are injectable for offline callers."""

    def __init__(
        self,
        host_data_dir: Path,
        *,
        fetch: Callable[[str], Awaitable[bytes]] = default_fetch,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._host_data_dir = host_data_dir
        self._fetch = fetch
        self._clock = clock

    async def stage(self, user_id: str, url: str) -> str:
        if not _USER_ID_RE.fullmatch(user_id):
            raise StageError("invalid_user_id")
        _validate_url(url)
        try:
            async with asyncio.timeout(FETCH_TIMEOUT_SECONDS):
                data = await self._fetch(url)
        except StageError:
            raise
        except Exception:
            raise StageError("download_failed") from None
        if len(data) > MAX_IMAGE_BYTES:
            raise StageError("too_large")
        extension = _extension(data)
        stamp = datetime.fromtimestamp(self._clock(), UTC).strftime("%Y%m%d-%H%M%S")
        try:
            with _inbox_fd(workspace_dir(self._host_data_dir, user_id)) as fd:
                number = 1
                while True:
                    name = f"{stamp}-{number}.{extension}"
                    try:
                        image_fd = os.open(name, _NEW_FLAGS, 0o644, dir_fd=fd)
                    except FileExistsError:
                        number += 1
                        continue
                    break
                try:
                    with os.fdopen(image_fd, "wb") as image:
                        os.fchmod(image.fileno(), 0o644)
                        image.write(data)
                except BaseException:
                    os.unlink(name, dir_fd=fd)
                    raise
                _prune(fd)
        except OSError as exc:
            code = "unsafe_path" if exc.errno in {errno.ELOOP, errno.ENOTDIR} else "storage_failed"
            raise StageError(code) from None
        return f"/workspace/inbox/{name}"
