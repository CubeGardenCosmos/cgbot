"""MuseScore 客户端:搜索、乐谱信息与文件下载。

页面获取顺序:1) 普通 HTTP;遇到 403/429/503 或挑战页时 2) 无头 Chromium 通过挑战并读取渲染页面。
下载 3):在页面上下文内调用 /api/jmuse,token 由页面构建脚本中的 secret 推导,文件经浏览器上下文拉取。
secret 顺序:本地缓存 -> 从页面构建脚本发现。jmuse 以 HTTP 422 拒绝令牌时清除缓存,
并改用字面量 LEGACY_SECRET(从不写入缓存)。
每次 422 之后等待 RETRY_422_DELAY_S 秒再重试,总共至多 JMUSE_TRIES 次,之后 download_failed。
搜索的挑战等待较短(SEARCH_CHALLENGE_WAIT_S);挑战未通过即返回 blocked,不再换用镜像或其他通道重试。
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from cgdl.browser import CHALLENGE_WAIT_S, DEFAULT_CHROMIUM, BrowserSession
from cgdl.errors import (
    BLOCKED,
    DOWNLOAD_FAILED,
    NOT_FOUND,
    PARSE_FAILED,
    UNSUPPORTED_KIND,
    CgdlError,
)
from cgdl.files import atomic_write, sanitize_stem
from cgdl.http import MAX_BYTES, MIN_INTERVAL_S, PlainHttp, Throttle, blocked_status
from cgdl.jmuse import (
    candidate_build_urls,
    discover_token_secret,
    jmuse_url,
    make_token,
    parse_jmuse,
)
from cgdl.parse import (
    ScoreHit,
    ScorePage,
    is_challenge,
    is_search_page,
    parse_score_page,
    parse_search_html,
)
from cgdl.secretcache import SecretCache, default_cache_dir
from cgdl.urls import ScoreRef, check_allowed, search_url

MAX_PAGES = 200
MAX_SCRIPT_BYTES = 8 * 1024 * 1024
# 搜索页的 Cloudflare 挑战等待时间;下载相关页面仍使用 CHALLENGE_WAIT_S(60 s)。
SEARCH_CHALLENGE_WAIT_S = 15.0
# jmuse 422 拒绝令牌:每次下载请求最多发起 JMUSE_TRIES 次(含首次),重试前等待 RETRY_422_DELAY_S 秒。
JMUSE_TRIES = 3
RETRY_422_DELAY_S = 2.0
# 字面量回退 secret:仅在发现的 secret 被 422 拒绝之后使用;从不写入缓存。
LEGACY_SECRET = "9654,4e"
# kind -> (jmuse type, 文件扩展名)
KINDS: dict[str, tuple[str, str]] = {
    "mid": ("midi", "mid"),
    "mxl": ("mxl", "mxl"),
    "mp3": ("mp3", "mp3"),
    "pdf": ("img", "pdf"),
    "png": ("img", "png"),
}


class PageBrowser(Protocol):
    """浏览器通道的最小接口;BrowserSession 实现它,测试可替换为假实现。"""

    def start(self) -> None: ...
    def close(self) -> None: ...
    def open(self, url: str, challenge_wait_s: float) -> tuple[int | None, str]: ...
    def page_fetch(self, url: str, headers: dict[str, str]) -> tuple[int, str]: ...
    def script_sources(self) -> list[str]: ...
    def fetch_text(self, url: str, max_bytes: int) -> str: ...
    def fetch_bytes(self, url: str, max_bytes: int) -> bytes: ...
    def render_pdf(self, pages: list[bytes], max_bytes: int) -> bytes: ...
    def render_png(self, svg: bytes, max_bytes: int) -> bytes: ...


BrowserFactory = Callable[[Throttle], PageBrowser]


@dataclass
class _Session:
    """单次下载的状态:页面浏览器与当前使用的 secret(422 后改用字面量,之后的请求沿用)。"""

    browser: PageBrowser
    secret: str


def _default_browser(throttle: Throttle) -> PageBrowser:
    executable = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or DEFAULT_CHROMIUM
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or None
    return BrowserSession(throttle, executable=executable, proxy=proxy)


def _check_format(kind: str, data: bytes) -> None:
    if kind == "mid":
        valid = data.startswith(b"MThd")
    elif kind == "mxl":
        valid = data.startswith(b"PK\x03\x04")
    elif kind == "mp3":
        valid = data.startswith(b"ID3") or (
            len(data) > 1 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0
        )
    elif kind == "png":
        valid = data.startswith(b"\x89PNG\r\n\x1a\n")
    else:
        valid = data.startswith(b"%PDF")
    if not valid:
        raise CgdlError(DOWNLOAD_FAILED, f"downloaded data is not a valid {kind} file")


class MuseScoreClient:
    def __init__(
        self,
        *,
        throttle: Throttle | None = None,
        plain: PlainHttp | None = None,
        browser_factory: BrowserFactory | None = None,
        cache: SecretCache | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._throttle = throttle if throttle is not None else Throttle(MIN_INTERVAL_S)
        self._plain = plain if plain is not None else PlainHttp(self._throttle)
        self._browser_factory: BrowserFactory = browser_factory or _default_browser
        self._cache = cache if cache is not None else SecretCache(default_cache_dir())
        self._sleep = sleep
        self._browser: PageBrowser | None = None
        self._browser_url: str | None = None

    def __enter__(self) -> MuseScoreClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._browser is not None:
            self._browser.close()
            self._browser = None
        self._plain.close()

    def search(self, query: str, limit: int) -> list[ScoreHit]:
        html = self._load_html(search_url(query), SEARCH_CHALLENGE_WAIT_S)
        hits = parse_search_html(html)
        if not hits and not is_search_page(html):
            raise CgdlError(PARSE_FAILED, "search page layout not recognised")
        return hits[:limit]

    def info(self, ref: ScoreRef) -> ScorePage:
        return parse_score_page(self._load_html(ref.url), ref.score_id)

    def get(self, ref: ScoreRef, kind: str, out_dir: Path, page: int = 1) -> dict[str, object]:
        if kind not in KINDS:
            raise CgdlError(
                UNSUPPORTED_KIND, f"unsupported kind {kind!r}; use mid, mxl, mp3, pdf or png"
            )
        html = self._load_html(ref.url)
        score_page = parse_score_page(html, ref.score_id)
        browser = self._browser_at(ref.url)
        build_urls = candidate_build_urls(html, browser.script_sources())
        session = _Session(browser=browser, secret=self._load_secret(browser, build_urls))
        jtype, ext = KINDS[kind]
        pages: int | None = None
        if kind == "pdf":
            data, pages = self._pdf(session, ref.score_id)
        elif kind == "png":
            data = self._png(session, ref.score_id, page)
        else:
            file_url = self._jmuse(session, ref.score_id, jtype, 0)
            if file_url is None:
                raise CgdlError(NOT_FOUND, f"MuseScore offers no {kind} file for this score")
            data = browser.fetch_bytes(file_url, MAX_BYTES)
        _check_format(kind, data)
        stem = sanitize_stem(score_page.title, ref.score_id)
        name = f"{stem}-p{page}" if kind == "png" else stem
        path = out_dir / f"{name}.{ext}"
        atomic_write(path, data)
        result: dict[str, object] = {
            "kind": kind,
            "path": str(path),
            "bytes": len(data),
            "score_id": ref.score_id,
            "title": score_page.title,
            "url": ref.url,
        }
        if pages is not None:
            result["pages"] = pages
        return result

    def _load_html(self, url: str, challenge_wait_s: float = CHALLENGE_WAIT_S) -> str:
        fetched = self._plain.get(url)
        if fetched.status == 404:
            raise CgdlError(NOT_FOUND, "page returned 404")
        if not blocked_status(fetched.status, fetched.html, fetched.cf_mitigated):
            if fetched.status != 200:
                raise CgdlError(DOWNLOAD_FAILED, f"HTTP {fetched.status} from musescore.com")
            return fetched.html
        return self._open_in_browser(url, challenge_wait_s)

    def _open_in_browser(self, url: str, challenge_wait_s: float) -> str:
        """在浏览器中打开 url;挑战未通过时返回 blocked,不做其他重试。"""
        browser = self._ensure_browser()
        _, html = browser.open(url, challenge_wait_s)
        if is_challenge(html, None):
            raise CgdlError(
                BLOCKED, f"Cloudflare challenge did not clear within {challenge_wait_s:g} s"
            )
        self._browser_url = url
        return html

    def _browser_at(self, url: str) -> PageBrowser:
        """返回已位于 url 页面的浏览器;下载必须在该页面的上下文中发起。"""
        browser = self._ensure_browser()
        if self._browser_url != url:
            self._open_in_browser(url, CHALLENGE_WAIT_S)
        return browser

    def _ensure_browser(self) -> PageBrowser:
        if self._browser is None:
            browser = self._browser_factory(self._throttle)
            browser.start()
            self._browser = browser
        return self._browser

    def _load_secret(self, browser: PageBrowser, build_urls: list[str]) -> str:
        cached = self._cache.load()
        if cached is not None:
            return cached[1]
        secret = self._discover(browser, build_urls)
        if secret is None:
            raise CgdlError(PARSE_FAILED, "jmuse token secret not found in page scripts")
        return secret

    def _discover(self, browser: PageBrowser, build_urls: list[str]) -> str | None:
        """从构建脚本发现 secret 并写入缓存;未命中返回 None。"""
        found = discover_token_secret(
            build_urls, lambda url: browser.fetch_text(url, MAX_SCRIPT_BYTES)
        )
        if found is None:
            return None
        build_url, secret = found
        self._cache.store(build_url, secret)
        return secret

    def _send(
        self, browser: PageBrowser, score_id: int, jtype: str, index: int, secret: str
    ) -> tuple[int, str]:
        return browser.page_fetch(
            jmuse_url(score_id, jtype, index),
            {"authorization": make_token(score_id, jtype, index, secret)},
        )

    def _jmuse(self, session: _Session, score_id: int, jtype: str, index: int) -> str | None:
        status, body = self._send(session.browser, score_id, jtype, index, session.secret)
        tries = 1
        while status == 422 and tries < JMUSE_TRIES:
            self._sleep(RETRY_422_DELAY_S)
            self._fall_back_to_legacy(session)
            status, body = self._send(session.browser, score_id, jtype, index, session.secret)
            tries += 1
        if status == 422:
            raise CgdlError(
                DOWNLOAD_FAILED, f"jmuse rejected the token (HTTP 422) in {JMUSE_TRIES} tries"
            )
        try:
            payload: object = json.loads(body)
        except ValueError as exc:
            raise CgdlError(PARSE_FAILED, "jmuse returned non-JSON") from exc
        found = parse_jmuse(status, payload)
        return None if found is None else check_allowed(found, DOWNLOAD_FAILED)

    def _fall_back_to_legacy(self, session: _Session) -> None:
        """令牌被 422 拒绝:清除缓存的 secret,改用字面量 LEGACY_SECRET;字面量不写入缓存。"""
        self._cache.clear()
        session.secret = LEGACY_SECRET

    def _pdf(self, session: _Session, score_id: int) -> tuple[bytes, int]:
        pages: list[bytes] = []
        total = 0
        for index in range(MAX_PAGES):
            file_url = self._jmuse(session, score_id, "img", index)
            if file_url is None:
                break
            data = session.browser.fetch_bytes(file_url, MAX_BYTES - total)
            total += len(data)
            pages.append(data)
        else:
            raise CgdlError(DOWNLOAD_FAILED, f"score has more than {MAX_PAGES} pages")
        if not pages:
            raise CgdlError(NOT_FOUND, "MuseScore offers no page images for this score")
        return session.browser.render_pdf(pages, MAX_BYTES), len(pages)

    def _png(self, session: _Session, score_id: int, page: int) -> bytes:
        file_url = self._jmuse(session, score_id, "img", page - 1)
        if file_url is None:
            raise CgdlError(NOT_FOUND, f"MuseScore offers no page {page} for this score")
        svg = session.browser.fetch_bytes(file_url, MAX_BYTES)
        return session.browser.render_png(svg, MAX_BYTES)
