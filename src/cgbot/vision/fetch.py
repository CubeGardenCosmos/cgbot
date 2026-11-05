"""SSRF-safe bounded image download shared by sandbox staging and chat vision.

Only public http(s) URLs are fetched: no proxies, no redirects, every resolved address is checked,
and the body is bounded after decompression. Failures carry a stable code and never contain the
remote URL (it may embed credentials), so callers can log the code alone.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult
from aiohttp.resolver import ThreadedResolver

MAX_IMAGE_BYTES = 20 * 1024 * 1024
FETCH_TIMEOUT_SECONDS = 30

Fetch = Callable[[str], Awaitable[bytes]]


class ImageError(Exception):
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
        raise ImageError("blocked_address")


def validate_url(url: str) -> None:
    if not url or any(ord(char) <= 32 or 127 <= ord(char) <= 159 for char in url) or "\\" in url:
        raise ImageError("invalid_url")
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
        raise ImageError("invalid_url") from None
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
                raise ImageError("blocked_address") from None
            _check_address(result["host"])
        if not results:
            raise ImageError("download_failed")
        return results

    async def close(self) -> None:
        await self._resolver.close()


async def default_fetch(url: str) -> bytes:
    """Fetch without proxies, redirects or unchecked DNS, bounded after decompression."""
    validate_url(url)
    resolver = _PublicResolver()
    try:
        connector = aiohttp.TCPConnector(resolver=resolver, use_dns_cache=False)
        timeout = aiohttp.ClientTimeout(total=30, connect=10, sock_read=10)
        async with aiohttp.ClientSession(
            connector=connector, timeout=timeout, trust_env=False
        ) as session:
            async with session.get(url, allow_redirects=False) as response:
                if response.status != 200:
                    raise ImageError("download_failed")
                if (
                    response.content_length is not None
                    and response.content_length > MAX_IMAGE_BYTES
                ):
                    raise ImageError("too_large")
                data = bytearray()
                async for chunk in response.content.iter_chunked(64 * 1024):
                    if len(data) + len(chunk) > MAX_IMAGE_BYTES:
                        raise ImageError("too_large")
                    data.extend(chunk)
                return bytes(data)
    except ImageError:
        raise
    except Exception:
        raise ImageError("download_failed") from None
    finally:
        await resolver.close()


async def fetch_image(url: str, fetch: Fetch = default_fetch) -> bytes:
    """Validate ``url``, fetch it through ``fetch`` (injectable) and enforce time and size caps."""
    validate_url(url)
    try:
        async with asyncio.timeout(FETCH_TIMEOUT_SECONDS):
            data = await fetch(url)
    except ImageError:
        raise
    except Exception:
        raise ImageError("download_failed") from None
    if len(data) > MAX_IMAGE_BYTES:
        raise ImageError("too_large")
    return data


def sniff_extension(data: bytes) -> str:
    """File extension of a PNG/JPEG/WebP/GIF by magic bytes; anything else is rejected."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    raise ImageError("unsupported_image")
