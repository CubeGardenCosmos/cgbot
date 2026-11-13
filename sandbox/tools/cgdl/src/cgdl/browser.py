"""策略 2 与 3:Playwright 驱动的无头 Chromium。

- open:导航并等待 Cloudflare 挑战通过,返回渲染后的 HTML。
- page_fetch:在页面上下文内 fetch(携带页面的 Cookie 与来源),用于 /api/jmuse。
- fetch_bytes / fetch_text:经 BrowserContext.request 拉取签名文件,共享同一 Cookie 罐。
- render_pdf:把各页 SVG 嵌入 A4 文档,由 Chromium 打印为 PDF。
- render_png:把单页 SVG 嵌入页面,对图片元素截图得到 PNG(单页大小上限与其他 kind 相同)。

所有网络请求都经过同一限速器。浏览器只允许白名单主机,其余请求直接中止。
"""

from __future__ import annotations

import base64
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlsplit

from cgdl.errors import BLOCKED, BROWSER_UNAVAILABLE, DOWNLOAD_FAILED, NOT_FOUND, CgdlError
from cgdl.http import Throttle
from cgdl.parse import is_challenge_title, is_not_found_page
from cgdl.urls import is_allowed_url

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Page, Playwright, Route

DEFAULT_CHROMIUM = "/usr/bin/chromium"
NAV_TIMEOUT_MS = 60_000
FETCH_TIMEOUT_MS = 60_000
CHALLENGE_WAIT_S = 60.0
LAUNCH_ARGS = ("--no-sandbox", "--disable-blink-features=AutomationControlled")
_FETCH_JS = (
    "async ([url, headers]) => {"
    " const r = await fetch(url, { headers, credentials: 'include' });"
    " return [r.status, await r.text()];"
    " }"
)
_PDF_HEAD = (
    '<!doctype html><html><head><meta charset="utf-8"><style>'
    "@page { size: A4; margin: 0; }"
    "html, body { margin: 0; padding: 0; }"
    "img { display: block; width: 210mm; height: 297mm; object-fit: contain; break-after: page; }"
    "</style></head><body>"
)

_PNG_HEAD = (
    '<!doctype html><html><head><meta charset="utf-8"><style>'
    "html, body { margin: 0; padding: 0; background: #fff; } img { display: block; }"
    "</style></head><body>"
)


def proxy_settings(url: str) -> dict[str, str]:
    """把 HTTPS_PROXY 形式的 URL 转为 Playwright 代理设置(用户信息拆出为 username/password)。"""
    parts = urlsplit(url)
    server = f"{parts.scheme}://{parts.hostname}"
    if parts.port is not None:
        server += f":{parts.port}"
    settings = {"server": server}
    if parts.username is not None:
        settings["username"] = unquote(parts.username)
    if parts.password is not None:
        settings["password"] = unquote(parts.password)
    return settings


def svg_img(svg: bytes) -> str:
    return f'<img src="data:image/svg+xml;base64,{base64.b64encode(svg).decode("ascii")}" alt="">'


def pdf_document(pages: list[bytes]) -> str:
    return _PDF_HEAD + "".join(svg_img(page) for page in pages) + "</body></html>"


def _route_request(route: Route) -> None:
    url = route.request.url
    if is_allowed_url(url) or url.startswith(("data:", "blob:")):
        route.continue_()
    else:
        route.abort()


class BrowserSession:
    def __init__(
        self,
        throttle: Throttle,
        *,
        executable: str = DEFAULT_CHROMIUM,
        proxy: str | None = None,
    ) -> None:
        self._throttle = throttle
        self._executable = executable
        self._proxy = proxy
        self._playwright: Playwright | None = None
        self._browser: Any = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    def start(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise CgdlError(BROWSER_UNAVAILABLE, "playwright is not installed") from exc
        playwright = sync_playwright().start()
        options: dict[str, Any] = {
            "executable_path": self._executable,
            "headless": True,
            "args": list(LAUNCH_ARGS),
        }
        if self._proxy:
            options["proxy"] = proxy_settings(self._proxy)
        try:
            browser = playwright.chromium.launch(**options)
        except Exception as exc:
            playwright.stop()
            raise CgdlError(
                BROWSER_UNAVAILABLE,
                f"cannot launch Chromium at {self._executable} ({type(exc).__name__})",
            ) from exc
        # 去掉 HeadlessChrome 标识:使用与真实版本一致的 UA。
        user_agent = (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{browser.version} Safari/537.36"
        )
        context = browser.new_context(
            user_agent=user_agent,
            locale="en-US",
            viewport={"width": 1366, "height": 900},
        )
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )
        context.route("**/*", _route_request)
        self._playwright = playwright
        self._browser = browser
        self._context = context
        self._page = context.new_page()

    def close(self) -> None:
        if self._context is not None:
            self._context.close()
            self._context = None
        if self._browser is not None:
            self._browser.close()
            self._browser = None
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None
        self._page = None

    def open(self, url: str, challenge_wait_s: float = CHALLENGE_WAIT_S) -> tuple[int | None, str]:
        page = self._live_page()
        self._throttle.wait()
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        except Exception as exc:
            raise CgdlError(
                DOWNLOAD_FAILED, f"page navigation failed ({type(exc).__name__})"
            ) from exc
        status = response.status if response is not None else None
        self._wait_for_challenge(page, challenge_wait_s)
        html = page.content()
        if status == 404 or is_not_found_page(html):
            raise CgdlError(NOT_FOUND, "page returned 404")
        return status, html

    def page_fetch(self, url: str, headers: dict[str, str]) -> tuple[int, str]:
        page = self._live_page()
        self._throttle.wait()
        try:
            status, body = page.evaluate(_FETCH_JS, [url, headers])
        except Exception as exc:
            raise CgdlError(
                DOWNLOAD_FAILED, f"in-page request failed ({type(exc).__name__})"
            ) from exc
        return int(status), str(body)

    def script_sources(self) -> list[str]:
        page = self._live_page()
        sources = page.evaluate(
            "() => Array.from(document.scripts).map((s) => s.src).filter(Boolean)"
        )
        return [str(item) for item in sources]

    def fetch_bytes(self, url: str, max_bytes: int) -> bytes:
        context = self._live_context()
        self._throttle.wait()
        try:
            response = context.request.get(url, timeout=FETCH_TIMEOUT_MS)
        except Exception as exc:
            raise CgdlError(DOWNLOAD_FAILED, f"file request failed ({type(exc).__name__})") from exc
        if not response.ok:
            raise CgdlError(DOWNLOAD_FAILED, f"HTTP {response.status} from file host")
        declared = response.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            raise CgdlError(DOWNLOAD_FAILED, "file exceeds the size cap")
        body = response.body()
        if len(body) > max_bytes:
            raise CgdlError(DOWNLOAD_FAILED, "file exceeds the size cap")
        return body

    def fetch_text(self, url: str, max_bytes: int) -> str:
        return self.fetch_bytes(url, max_bytes).decode("utf-8", errors="replace")

    def render_pdf(self, pages: list[bytes], max_bytes: int) -> bytes:
        page = self._live_page()
        page.set_content(pdf_document(pages), wait_until="load")
        data = bytes(page.pdf(format="A4", print_background=True, prefer_css_page_size=True))
        if len(data) > max_bytes:
            raise CgdlError(DOWNLOAD_FAILED, "rendered PDF exceeds the size cap")
        return data

    def render_png(self, svg: bytes, max_bytes: int) -> bytes:
        page = self._live_page()
        page.set_content(_PNG_HEAD + svg_img(svg) + "</body></html>", wait_until="load")
        try:
            data = page.locator("img").screenshot(type="png", timeout=FETCH_TIMEOUT_MS)
        except Exception as exc:
            raise CgdlError(
                DOWNLOAD_FAILED, f"PNG rendering failed ({type(exc).__name__})"
            ) from exc
        if len(data) > max_bytes:
            raise CgdlError(DOWNLOAD_FAILED, "rendered PNG exceeds the size cap")
        return data

    def _wait_for_challenge(self, page: Page, wait_s: float) -> None:
        deadline = time.monotonic() + wait_s
        while True:
            try:
                title = page.title()
            except Exception:
                # 挑战通过后页面会刷新,刷新期间读取标题可能失败,重试即可。
                title = ""
            if title and not is_challenge_title(title):
                return
            if time.monotonic() >= deadline:
                raise CgdlError(BLOCKED, f"Cloudflare challenge did not clear within {wait_s:g} s")
            page.wait_for_timeout(2000)

    def _live_page(self) -> Page:
        if self._page is None:
            raise CgdlError(BROWSER_UNAVAILABLE, "browser session is not started")
        return self._page

    def _live_context(self) -> BrowserContext:
        if self._context is None:
            raise CgdlError(BROWSER_UNAVAILABLE, "browser session is not started")
        return self._context
