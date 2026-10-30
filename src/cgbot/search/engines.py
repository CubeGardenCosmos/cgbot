"""Web search engines (Bing, Baidu, Bilibili) scraped from public endpoints.

Each engine owns one transport (an aiohttp session with its own cookie jar), so cookies from one
site never leak into another. Parsers are pure functions over the response body and are tested
against recorded fixtures. Failures surface as :class:`EngineError` carrying a stable code; the
remote body is never logged or returned.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol

import aiohttp

from cgbot.search.models import (
    E_BAIDU_BLOCKED,
    E_BILI_RISK,
    E_HTTP,
    E_NETWORK,
    E_NO_RESULT,
    E_PARSE,
    E_TIMEOUT,
    SearchResult,
)

log = logging.getLogger(__name__)

DEFAULT_UA: Final = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
# Baidu checks request headers strictly; it gets its own, newer browser fingerprint.
BAIDU_UA: Final = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
ACCEPT_LANGUAGE: Final = "zh-CN,zh;q=0.9"

BING_FRESHNESS: Final[Mapping[str, str]] = {
    "day": "ez1",
    "week": "ez2",
    "month": "ez3",
    "year": "ez4",
}

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


class EngineError(Exception):
    """A single engine failed; ``code`` is one of the stable ``E_*`` codes."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class HttpReply:
    """Status and text body. The body is empty for non-200 responses."""

    status: int
    text: str


class Transport(Protocol):
    async def get(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None,
        headers: Mapping[str, str],
        timeout: float,  # noqa: ASYNC109 - per-request budget is part of the Transport contract
    ) -> HttpReply: ...

    async def close(self) -> None: ...


class SearchEngine(Protocol):
    async def search(self, query: str, count: int, freshness: str = "") -> list[SearchResult]: ...

    async def close(self) -> None: ...


class AiohttpTransport:
    """Direct (no proxy) GET over one lazily created aiohttp session.

    ``keep_cookies`` enables an unsafe cookie jar so cookies set by IP-literal or domain-less
    responses (Baidu, Bilibili warmup) are kept for later requests on this transport.
    """

    def __init__(self, *, user_agent: str, keep_cookies: bool) -> None:
        self._user_agent = user_agent
        self._keep_cookies = keep_cookies
        self._session: aiohttp.ClientSession | None = None

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            jar = aiohttp.CookieJar(unsafe=True) if self._keep_cookies else None
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": self._user_agent, "Accept-Language": ACCEPT_LANGUAGE},
                cookie_jar=jar,
            )
        return self._session

    async def get(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None,
        headers: Mapping[str, str],
        timeout: float,  # noqa: ASYNC109 - per-request budget is part of the Transport contract
    ) -> HttpReply:
        session = self._ensure_session()
        async with session.get(
            url,
            params=dict(params) if params is not None else None,
            headers=dict(headers),
            timeout=aiohttp.ClientTimeout(total=timeout),
            allow_redirects=True,
        ) as resp:
            if resp.status != 200:
                return HttpReply(status=resp.status, text="")
            return HttpReply(status=200, text=await resp.text(errors="ignore"))

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None


def strip_tags(raw: str) -> str:
    """Remove HTML tags, unescape entities, collapse whitespace."""
    if not raw:
        return ""
    return _WS_RE.sub(" ", html.unescape(_TAG_RE.sub("", raw))).strip()


def _is_http_url(url: str) -> bool:
    return url.startswith(("http://", "https://"))


def _require(results: list[SearchResult], empty_code: str) -> list[SearchResult]:
    if not results:
        raise EngineError(empty_code)
    return results


async def _get(
    transport: Transport,
    url: str,
    *,
    headers: Mapping[str, str],
    timeout: float,  # noqa: ASYNC109 - per-request budget is part of the Transport contract
    params: Mapping[str, str] | None = None,
    http_codes: Mapping[int, str] | None = None,
) -> HttpReply:
    """GET with transport errors and non-200 statuses mapped to stable codes."""
    try:
        reply = await transport.get(url, params=params, headers=headers, timeout=timeout)
    except TimeoutError as exc:
        raise EngineError(E_TIMEOUT) from exc
    except aiohttp.ClientError as exc:
        raise EngineError(E_NETWORK) from exc
    if reply.status != 200:
        raise EngineError((http_codes or {}).get(reply.status, E_HTTP))
    return reply


async def _warm(
    transport: Transport,
    url: str,
    headers: Mapping[str, str],
    timeout: float,  # noqa: ASYNC109 - per-request budget is part of the Transport contract
) -> None:
    """Best-effort cookie warmup: failure only makes later requests more likely to be challenged."""
    try:
        await transport.get(url, params=None, headers=headers, timeout=timeout)
    except (TimeoutError, aiohttp.ClientError):
        log.debug("search warmup skipped")


# ---------------------------------------------------------------------------
# Bing
# ---------------------------------------------------------------------------

_BING_BLOCK_RE = re.compile(r'<li class="b_algo".*?</li>', re.S)
_BING_LINK_RE = re.compile(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_BING_SNIPPET_RE = re.compile(r"<p[^>]*>(.*?)</p>", re.S)


def parse_bing_web(body: str, count: int) -> list[SearchResult]:
    """Organic results from ``li.b_algo`` blocks; blocks without a titled http(s) link dropped."""
    results: list[SearchResult] = []
    for block in _BING_BLOCK_RE.findall(body):
        link = _BING_LINK_RE.search(block)
        if link is None:
            continue
        url = html.unescape(link.group(1))
        title = strip_tags(link.group(2))
        if not title or not _is_http_url(url):
            continue
        snippet_m = _BING_SNIPPET_RE.search(block)
        snippet = strip_tags(snippet_m.group(1)) if snippet_m else ""
        results.append(SearchResult(title=title, url=url, snippet=snippet, source="bing"))
        if len(results) >= count:
            break
    return results


class BingEngine:
    name: Final = "bing"
    _BASE: Final = "https://cn.bing.com"

    def __init__(self, transport: Transport, timeout: float) -> None:
        self._transport = transport
        self._timeout = timeout

    async def search(self, query: str, count: int, freshness: str = "") -> list[SearchResult]:
        params = {"q": query, "count": str(count), "setlang": "zh-CN", "ensearch": "0"}
        if freshness in BING_FRESHNESS:
            params["filters"] = f'ex1:"{BING_FRESHNESS[freshness]}"'
        reply = await _get(
            self._transport,
            f"{self._BASE}/search",
            params=params,
            headers={},
            timeout=self._timeout,
        )
        return _require(parse_bing_web(reply.text, count), E_NO_RESULT)

    async def close(self) -> None:
        await self._transport.close()


# ---------------------------------------------------------------------------
# Baidu
# ---------------------------------------------------------------------------

# Result cards are anchored on ``mu="..."`` (the real target URL) so ad slots and empty
# containers are not mistaken for result boundaries.
_BAIDU_CARD_RE = re.compile(
    r'<div[^>]*class="result c-container[^"]*"(?=[^>]*\bmu=")'
    r"(.*?)"
    r'(?=<div[^>]*class="result c-container[^"]*"[^>]*\bmu="|</body>|$)',
    re.S,
)
_BAIDU_MU_RE = re.compile(r'\bmu="([^"]+)"')
_BAIDU_H3_RE = re.compile(r"<h3[^>]*>(.*?)</h3>", re.S)
_BAIDU_SNIPPET_RE = re.compile(
    r'class="[^"]*(?:content-right|c-abstract|content-abstract)[^"]*"[^>]*>(.*?)</div>',
    re.S,
)
_BAIDU_BLOCK_MARKERS: Final = ("安全验证", "timeout-title", "wappass")


def is_baidu_blocked(body: str) -> bool:
    """Security-check pages are short and carry a fixed marker; full result pages never match."""
    return len(body) < 5000 and any(marker in body for marker in _BAIDU_BLOCK_MARKERS)


def parse_baidu_web(body: str, count: int) -> list[SearchResult]:
    """Results from ``result c-container`` cards. Raises ``E_BAIDU_BLOCKED`` on a security page.

    Returns an empty list when no usable card exists; the engine maps that to ``E_BAIDU_BLOCKED``
    too, because a Baidu page without results is almost always a degraded (blocked) response.
    """
    if is_baidu_blocked(body):
        raise EngineError(E_BAIDU_BLOCKED)
    results: list[SearchResult] = []
    for card in _BAIDU_CARD_RE.findall(body):
        mu = _BAIDU_MU_RE.search(card)
        h3 = _BAIDU_H3_RE.search(card)
        title = strip_tags(h3.group(1)) if h3 else ""
        url = html.unescape(mu.group(1)) if mu else ""
        if not title or not _is_http_url(url):
            continue
        snip = _BAIDU_SNIPPET_RE.search(card)
        snippet = strip_tags(snip.group(1)) if snip else ""
        results.append(SearchResult(title=title, url=url, snippet=snippet, source="baidu"))
        if len(results) >= count:
            break
    return results


class BaiduEngine:
    """Warms up with the home page once (BAIDUID etc.), then queries ``/s``.

    A configured cookie header is sent as-is on every request.
    """

    name: Final = "baidu"
    _BASE: Final = "https://www.baidu.com"

    def __init__(self, transport: Transport, timeout: float, cookies: str = "") -> None:
        self._transport = transport
        self._timeout = timeout
        self._cookies = cookies.strip()
        self._warmed = False
        self._warm_lock = asyncio.Lock()

    def _headers(self, referer: str = "") -> dict[str, str]:
        headers = {
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Upgrade-Insecure-Requests": "1",
            "User-Agent": BAIDU_UA,
        }
        if referer:
            headers["Referer"] = referer
        if self._cookies:
            headers["Cookie"] = self._cookies
        return headers

    async def _warmup(self) -> None:
        async with self._warm_lock:
            if self._warmed or self._cookies:
                return
            self._warmed = True
            await _warm(self._transport, f"{self._BASE}/", self._headers(), self._timeout)

    async def search(self, query: str, count: int, freshness: str = "") -> list[SearchResult]:
        await self._warmup()
        params = {
            "ie": "utf-8",
            "f": "8",
            "rsv_bp": "1",
            "tn": "baidu",
            "wd": query,
            "rn": str(count),
        }
        reply = await _get(
            self._transport,
            f"{self._BASE}/s",
            params=params,
            headers=self._headers(referer=f"{self._BASE}/"),
            timeout=self._timeout,
        )
        return _require(parse_baidu_web(reply.text, count), E_BAIDU_BLOCKED)

    async def close(self) -> None:
        await self._transport.close()


# ---------------------------------------------------------------------------
# Bilibili
# ---------------------------------------------------------------------------

# API codes meaning risk control rather than a bad request.
_BILI_RISK_CODES: Final = frozenset({-412, -352})


def _bili_items(groups: Any) -> list[Any]:
    """Video items from ``search/type`` (flat list) or ``search/all/v2`` (grouped)."""
    if isinstance(groups, dict):
        video = groups.get("video")
        return video if isinstance(video, list) else []
    if not isinstance(groups, list) or not groups:
        return []
    if not all(isinstance(g, dict) for g in groups):
        return []
    # Flat list: each item carries its own bvid / arcurl.
    if any(g.get("bvid") or g.get("arcurl") for g in groups):
        return groups
    # Grouped list: take the video group's ``data``.
    for group in groups:
        if (group.get("result_type") or group.get("type")) != "video":
            continue
        inner = group.get("data")
        return inner if isinstance(inner, list) else [group]
    return []


def parse_bilibili_web(body: str, count: int) -> list[SearchResult]:
    """Video results with ``https://www.bilibili.com/video/<bvid>`` links and description.

    API risk codes raise ``E_BILI_RISK``; any other non-zero code or malformed JSON raises
    ``E_PARSE``.
    """
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise EngineError(E_PARSE) from exc
    if not isinstance(data, dict):
        raise EngineError(E_PARSE)
    code = data.get("code")
    if code != 0:
        raise EngineError(E_BILI_RISK if code in _BILI_RISK_CODES else E_PARSE)
    payload = data.get("data")
    if not isinstance(payload, dict):
        raise EngineError(E_PARSE)

    results: list[SearchResult] = []
    for item in _bili_items(payload.get("result") or []):
        if not isinstance(item, dict):
            continue
        bvid = str(item.get("bvid") or "").strip()
        url = f"https://www.bilibili.com/video/{bvid}" if bvid else str(item.get("arcurl") or "")
        title = strip_tags(str(item.get("title") or ""))
        if not title or not _is_http_url(url):
            continue
        description = str(item.get("description") or "").replace("\r", " ").replace("\n", " ")
        results.append(
            SearchResult(
                title=title,
                url=url,
                snippet=_WS_RE.sub(" ", description).strip(),
                source="bilibili",
                meta=_bili_meta(item),
            )
        )
        if len(results) >= count:
            break
    return results


def _bili_meta(item: dict[str, Any]) -> str:
    parts: list[str] = []
    author = item.get("author") or item.get("uname")
    if author:
        parts.append(f"UP主：{author}")
    play = item.get("play")
    if play not in ("", None):
        parts.append(f"播放：{play}")
    if item.get("duration"):
        parts.append(f"时长：{item['duration']}")
    if item.get("typename"):
        parts.append(f"分区：{item['typename']}")
    return "｜".join(parts)


class BilibiliEngine:
    """Public ``search/type`` API. Without configured cookies, one warmup visit to the main site
    first fetches the device cookies (buvid3) that keep the API from answering 412."""

    name: Final = "bilibili"
    _API_BASE: Final = "https://api.bilibili.com"
    _HOME: Final = "https://www.bilibili.com/"
    _SEARCH_SITE: Final = "https://search.bilibili.com"

    def __init__(self, transport: Transport, timeout: float, cookies: str = "") -> None:
        self._transport = transport
        self._timeout = timeout
        self._cookies = cookies.strip()
        self._warmed = False
        self._warm_lock = asyncio.Lock()

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Referer": f"{self._SEARCH_SITE}/",
            "Origin": self._SEARCH_SITE,
        }
        if self._cookies:
            headers["Cookie"] = self._cookies
        return headers

    async def _warmup(self) -> None:
        async with self._warm_lock:
            if self._warmed:
                return
            self._warmed = True
            if not self._cookies:
                await _warm(self._transport, self._HOME, {}, self._timeout)

    async def search(self, query: str, count: int, freshness: str = "") -> list[SearchResult]:
        await self._warmup()
        params = {"search_type": "video", "keyword": query, "page": "1"}
        reply = await _get(
            self._transport,
            f"{self._API_BASE}/x/web-interface/search/type",
            params=params,
            headers=self._headers(),
            timeout=self._timeout,
            http_codes={412: E_BILI_RISK},
        )
        return _require(parse_bilibili_web(reply.text, count), E_NO_RESULT)

    async def close(self) -> None:
        await self._transport.close()


def make_engine(name: str, *, timeout: float, cookies: str = "") -> SearchEngine:
    """Engine with its own transport. Only Baidu and Bilibili keep cookies (warmup + config)."""
    if name == "bing":
        return BingEngine(AiohttpTransport(user_agent=DEFAULT_UA, keep_cookies=False), timeout)
    if name == "baidu":
        transport = AiohttpTransport(user_agent=BAIDU_UA, keep_cookies=True)
        return BaiduEngine(transport, timeout, cookies)
    if name == "bilibili":
        transport = AiohttpTransport(user_agent=DEFAULT_UA, keep_cookies=True)
        return BilibiliEngine(transport, timeout, cookies)
    raise ValueError(f"unknown search engine: {name!r}")
