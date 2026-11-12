"""策略 1:普通 HTTP(带真实浏览器请求头的 httpx),以及全局限速器。

限速器被 HTTP 与浏览器两条通道共用:任意两次网络请求之间至少间隔 MIN_INTERVAL_S 秒。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from cgdl.errors import DOWNLOAD_FAILED, CgdlError
from cgdl.parse import is_challenge
from cgdl.urls import check_allowed

MIN_INTERVAL_S = 1.0
MAX_BYTES = 40 * 1024 * 1024
TIMEOUT_S = 30.0
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36"
)
BROWSER_HEADERS: dict[str, str] = {
    "User-Agent": USER_AGENT,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "sec-ch-ua": '"Chromium";v="140", "Not=A?Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Linux"',
}


class Throttle:
    """保证相邻两次网络请求之间至少间隔 interval 秒。时钟与睡眠可注入,便于测试。"""

    def __init__(
        self,
        interval: float = MIN_INTERVAL_S,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._interval = interval
        self._clock = clock
        self._sleep = sleep
        self._last: float | None = None

    def wait(self) -> None:
        if self._last is not None:
            remaining = self._last + self._interval - self._clock()
            if remaining > 0:
                self._sleep(remaining)
        self._last = self._clock()


@dataclass(frozen=True, slots=True)
class Fetched:
    status: int
    body: bytes
    cf_mitigated: str | None

    @property
    def html(self) -> str:
        return self.body.decode("utf-8", errors="replace")


class PlainHttp:
    """策略 1。被 Cloudflare 挑战时由调用方转入浏览器通道。"""

    def __init__(
        self,
        throttle: Throttle,
        *,
        transport: httpx.BaseTransport | None = None,
        max_bytes: int = MAX_BYTES,
    ) -> None:
        self._throttle = throttle
        self._max_bytes = max_bytes
        self._client = httpx.Client(
            headers=BROWSER_HEADERS,
            timeout=TIMEOUT_S,
            follow_redirects=True,
            transport=transport,
        )

    def get(self, url: str) -> Fetched:
        check_allowed(url, DOWNLOAD_FAILED)
        self._throttle.wait()
        try:
            with self._client.stream("GET", url) as response:
                check_allowed(str(response.url), DOWNLOAD_FAILED)
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > self._max_bytes:
                        raise CgdlError(DOWNLOAD_FAILED, "response exceeds the 40 MiB cap")
                    chunks.append(chunk)
                return Fetched(
                    status=response.status_code,
                    body=b"".join(chunks),
                    cf_mitigated=response.headers.get("cf-mitigated"),
                )
        except httpx.HTTPError as exc:
            raise CgdlError(DOWNLOAD_FAILED, f"HTTP request failed ({type(exc).__name__})") from exc

    def close(self) -> None:
        self._client.close()


def blocked_status(status: int, html: str, cf_mitigated: str | None) -> bool:
    """HTTP 层面判断是否需要浏览器通道:403/429/503 或挑战页。"""
    return status in (403, 429, 503) or is_challenge(html, cf_mitigated)
