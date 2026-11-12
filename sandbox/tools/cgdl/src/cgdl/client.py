"""MuseScore 客户端:搜索、乐谱信息与文件下载。

页面获取顺序:1) 普通 HTTP;遇到 403/429/503 或挑战页时 2) 无头 Chromium 通过挑战并读取渲染页面。
下载 3):在页面上下文内调用 /api/jmuse,token 由页面脚本推导,文件经浏览器上下文拉取。
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from cgdl.browser import DEFAULT_CHROMIUM, BrowserSession
from cgdl.errors import (
    DOWNLOAD_FAILED,
    NOT_FOUND,
    PARSE_FAILED,
    UNSUPPORTED_KIND,
    CgdlError,
)
from cgdl.files import atomic_write, sanitize_stem
from cgdl.http import MAX_BYTES, MIN_INTERVAL_S, PlainHttp, Throttle, blocked_status
from cgdl.jmuse import find_token_secret, jmuse_url, make_token, parse_jmuse
from cgdl.parse import (
    ScorePage,
    SearchHit,
    is_search_page,
    parse_score_page,
    parse_search,
)
from cgdl.urls import ScoreRef, check_allowed, is_allowed_url, search_url

MAX_PAGES = 200
MAX_SCRIPT_BYTES = 8 * 1024 * 1024
# kind -> (jmuse type, 文件扩展名)
KINDS: dict[str, tuple[str, str]] = {
    "mid": ("midi", "mid"),
    "mxl": ("mxl", "mxl"),
    "mp3": ("mp3", "mp3"),
    "pdf": ("img", "pdf"),
}
_BUILD_PATH = "/static/public/build/"


class PageBrowser(Protocol):
    """浏览器通道的最小接口;BrowserSession 实现它,测试可替换为假实现。"""

    def start(self) -> None: ...
    def close(self) -> None: ...
    def open(self, url: str) -> tuple[int | None, str]: ...
    def page_fetch(self, url: str, headers: dict[str, str]) -> tuple[int, str]: ...
    def script_sources(self) -> list[str]: ...
    def fetch_text(self, url: str, max_bytes: int) -> str: ...
    def fetch_bytes(self, url: str, max_bytes: int) -> bytes: ...
    def render_pdf(self, pages: list[bytes], max_bytes: int) -> bytes: ...


BrowserFactory = Callable[[Throttle], PageBrowser]


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
    ) -> None:
        self._throttle = throttle if throttle is not None else Throttle(MIN_INTERVAL_S)
        self._plain = plain if plain is not None else PlainHttp(self._throttle)
        self._browser_factory: BrowserFactory = browser_factory or _default_browser
        self._browser: PageBrowser | None = None
        self._browser_url: str | None = None
        self._secret: str | None = None

    def __enter__(self) -> MuseScoreClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._browser is not None:
            self._browser.close()
            self._browser = None
        self._plain.close()

    def search(self, query: str, limit: int) -> list[SearchHit]:
        html = self._load_html(search_url(query))
        hits = parse_search(html)
        if not hits and not is_search_page(html):
            raise CgdlError(PARSE_FAILED, "search page layout not recognised")
        return hits[:limit]

    def info(self, ref: ScoreRef) -> ScorePage:
        return parse_score_page(self._load_html(ref.url), ref.score_id)

    def get(self, ref: ScoreRef, kind: str, out_dir: Path) -> dict[str, object]:
        if kind not in KINDS:
            raise CgdlError(
                UNSUPPORTED_KIND, f"unsupported kind {kind!r}; use mid, mxl, mp3 or pdf"
            )
        page = parse_score_page(self._load_html(ref.url), ref.score_id)
        browser = self._browser_at(ref.url)
        secret = self._token_secret(browser)
        jtype, ext = KINDS[kind]
        pages: int | None = None
        if kind == "pdf":
            data, pages = self._pdf(browser, ref.score_id, secret)
        else:
            file_url = self._jmuse(browser, ref.score_id, jtype, 0, secret)
            if file_url is None:
                raise CgdlError(NOT_FOUND, f"MuseScore offers no {kind} file for this score")
            data = browser.fetch_bytes(file_url, MAX_BYTES)
        _check_format(kind, data)
        path = out_dir / f"{sanitize_stem(page.title, ref.score_id)}.{ext}"
        atomic_write(path, data)
        result: dict[str, object] = {
            "kind": kind,
            "path": str(path),
            "bytes": len(data),
            "score_id": ref.score_id,
            "title": page.title,
            "url": ref.url,
        }
        if pages is not None:
            result["pages"] = pages
        return result

    def _load_html(self, url: str) -> str:
        fetched = self._plain.get(url)
        if fetched.status == 404:
            raise CgdlError(NOT_FOUND, "page returned 404")
        if not blocked_status(fetched.status, fetched.html, fetched.cf_mitigated):
            if fetched.status != 200:
                raise CgdlError(DOWNLOAD_FAILED, f"HTTP {fetched.status} from musescore.com")
            return fetched.html
        browser = self._ensure_browser()
        _, html = browser.open(url)
        self._browser_url = url
        return html

    def _browser_at(self, url: str) -> PageBrowser:
        """返回已位于 url 页面的浏览器;下载必须在该页面的上下文中发起。"""
        browser = self._ensure_browser()
        if self._browser_url != url:
            browser.open(url)
            self._browser_url = url
        return browser

    def _ensure_browser(self) -> PageBrowser:
        if self._browser is None:
            browser = self._browser_factory(self._throttle)
            browser.start()
            self._browser = browser
        return self._browser

    def _token_secret(self, browser: PageBrowser) -> str:
        if self._secret is None:
            for src in browser.script_sources():
                if _BUILD_PATH not in urlsplit(src).path or not is_allowed_url(src):
                    continue
                secret = find_token_secret(browser.fetch_text(src, MAX_SCRIPT_BYTES))
                if secret is not None:
                    self._secret = secret
                    break
        if self._secret is None:
            raise CgdlError(PARSE_FAILED, "jmuse token secret not found in page scripts")
        return self._secret

    def _jmuse(
        self, browser: PageBrowser, score_id: int, jtype: str, index: int, secret: str
    ) -> str | None:
        url = jmuse_url(score_id, jtype, index)
        status, body = browser.page_fetch(
            url, {"authorization": make_token(score_id, jtype, index, secret)}
        )
        try:
            payload: object = json.loads(body)
        except ValueError as exc:
            raise CgdlError(PARSE_FAILED, "jmuse returned non-JSON") from exc
        found = parse_jmuse(status, payload)
        return None if found is None else check_allowed(found, DOWNLOAD_FAILED)

    def _pdf(self, browser: PageBrowser, score_id: int, secret: str) -> tuple[bytes, int]:
        pages: list[bytes] = []
        total = 0
        for index in range(MAX_PAGES):
            file_url = self._jmuse(browser, score_id, "img", index, secret)
            if file_url is None:
                break
            data = browser.fetch_bytes(file_url, MAX_BYTES - total)
            total += len(data)
            pages.append(data)
        else:
            raise CgdlError(DOWNLOAD_FAILED, f"score has more than {MAX_PAGES} pages")
        if not pages:
            raise CgdlError(NOT_FOUND, "MuseScore offers no page images for this score")
        return browser.render_pdf(pages, MAX_BYTES), len(pages)
