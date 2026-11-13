"""musescore-get --kind png 的离线测试:假浏览器替代 Chromium,不发起任何网络请求。"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fixtures import BUILD_JS, BUILD_URL, FAKE_SECRET, SCORE_HTML, jmuse_json

from cgdl import cli
from cgdl.browser import FETCH_TIMEOUT_MS, BrowserSession
from cgdl.cli import main
from cgdl.client import MuseScoreClient
from cgdl.errors import BAD_INPUT, DOWNLOAD_FAILED, NOT_FOUND, UNSUPPORTED_KIND, CgdlError
from cgdl.http import PlainHttp, Throttle
from cgdl.jmuse import make_token
from cgdl.secretcache import SecretCache

URL = "https://musescore.com/user/39593079/scores/6969530"
S3 = "https://s3w.musescore.com/musescore.scoredata/g/h/"
STEM = "Gymnopédie_No.1-6969530"
SVG_PAGES = [
    b"<svg xmlns='http://www.w3.org/2000/svg' id='p1'/>",
    b"<svg xmlns='http://www.w3.org/2000/svg' id='p2'/>",
]
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def _page_url(index: int) -> str:
    return f"{S3}page{index}.svg"


class FakeBrowser:
    """实现 client.PageBrowser 的假浏览器:jmuse img 按页索引返回 SVG 地址,render_png 记录输入。"""

    def __init__(self, pages: list[bytes]) -> None:
        self.files = {_page_url(index): svg for index, svg in enumerate(pages)}
        self.pages = len(pages)
        self.fetch_calls: list[tuple[str, dict[str, str]]] = []
        self.rendered: list[bytes] = []

    def start(self) -> None:
        pass

    def close(self) -> None:
        pass

    def open(self, url: str, challenge_wait_s: float) -> tuple[int | None, str]:
        return 200, SCORE_HTML

    def page_fetch(self, url: str, headers: dict[str, str]) -> tuple[int, str]:
        self.fetch_calls.append((url, headers))
        index = int(parse_qs(urlsplit(url).query)["index"][0])
        return 200, jmuse_json(_page_url(index) if index < self.pages else "")

    def script_sources(self) -> list[str]:
        return [BUILD_URL]

    def fetch_text(self, url: str, max_bytes: int) -> str:
        assert url == BUILD_URL
        return BUILD_JS

    def fetch_bytes(self, url: str, max_bytes: int) -> bytes:
        return self.files[url]

    def render_png(self, svg: bytes, max_bytes: int) -> bytes:
        self.rendered.append(svg)
        return PNG_BYTES


def _score_only(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=SCORE_HTML.encode())


def _install_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, browser: FakeBrowser) -> None:
    def factory() -> MuseScoreClient:
        return MuseScoreClient(
            throttle=Throttle(0.0, sleep=lambda _: None),
            plain=PlainHttp(
                Throttle(0.0, sleep=lambda _: None), transport=httpx.MockTransport(_score_only)
            ),
            browser_factory=lambda _: browser,
            cache=SecretCache(tmp_path / "cache"),
            sleep=lambda _: None,
        )

    monkeypatch.setattr(cli, "MuseScoreClient", factory)


def _no_client() -> MuseScoreClient:
    raise AssertionError("no client may be started for invalid input")


def _stderr_error(capsys: pytest.CaptureFixture[str]) -> str:
    payload = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert set(payload) == {"error", "message"}
    return str(payload["error"])


def test_png_defaults_to_first_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    browser = FakeBrowser(SVG_PAGES)
    _install_client(monkeypatch, tmp_path, browser)
    assert main(["musescore-get", URL, "--kind", "png", "-o", str(tmp_path)]) == 0
    payload = json.loads(capsys.readouterr().out)
    path = tmp_path / f"{STEM}-p1.png"
    assert set(payload) == {"kind", "path", "bytes", "score_id", "title", "url"}
    assert payload["kind"] == "png"
    assert payload["path"] == str(path)
    assert payload["bytes"] == len(PNG_BYTES)
    assert payload["score_id"] == 6969530
    assert payload["url"] == URL
    assert path.read_bytes() == PNG_BYTES
    assert browser.rendered == [SVG_PAGES[0]]
    assert browser.fetch_calls[-1][1] == {
        "authorization": make_token(6969530, "img", 0, FAKE_SECRET)
    }


def test_png_page_option_selects_that_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    browser = FakeBrowser(SVG_PAGES)
    _install_client(monkeypatch, tmp_path, browser)
    assert main(["musescore-get", URL, "--kind", "png", "--page", "2", "-o", str(tmp_path)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["path"] == str(tmp_path / f"{STEM}-p2.png")
    assert (tmp_path / f"{STEM}-p2.png").read_bytes() == PNG_BYTES
    assert browser.rendered == [SVG_PAGES[1]]
    assert browser.fetch_calls[-1][1] == {
        "authorization": make_token(6969530, "img", 1, FAKE_SECRET)
    }


def test_png_page_beyond_last_is_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CGDL_CACHE_DIR", str(tmp_path / "cache"))
    out = tmp_path / "out"
    out.mkdir()
    browser = FakeBrowser(SVG_PAGES)
    _install_client(monkeypatch, tmp_path, browser)
    assert main(["musescore-get", URL, "--kind", "png", "--page", "3", "-o", str(out)]) == 1
    assert _stderr_error(capsys) == NOT_FOUND
    assert browser.rendered == []
    assert list(out.iterdir()) == []


@pytest.mark.parametrize("page", ["0", "-1"])
def test_png_page_below_one_is_bad_input(
    page: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "MuseScoreClient", _no_client)
    assert main(["musescore-get", URL, "--kind", "png", "--page", page]) == 1
    assert _stderr_error(capsys) == BAD_INPUT


def test_page_option_without_png_is_bad_input(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "MuseScoreClient", _no_client)
    assert main(["musescore-get", URL, "--kind", "mid", "--page", "2"]) == 1
    assert _stderr_error(capsys) == BAD_INPUT


def test_unknown_kind_is_unsupported_before_any_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "MuseScoreClient", _no_client)
    assert main(["musescore-get", URL, "--kind", "gif", "-o", str(tmp_path)]) == 1
    assert _stderr_error(capsys) == UNSUPPORTED_KIND
    assert list(tmp_path.iterdir()) == []


class _Locator:
    def __init__(self, page: _Page) -> None:
        self._page = page

    def screenshot(self, **options: object) -> bytes:
        self._page.shots.append(options)
        return PNG_BYTES


class _Page:
    """替代 Playwright Page:只记录 set_content 的 HTML 与 img 元素截图参数。"""

    def __init__(self) -> None:
        self.html = ""
        self.shots: list[dict[str, object]] = []

    def set_content(self, html: str, *, wait_until: str) -> None:
        assert wait_until == "load"
        self.html = html

    def locator(self, selector: str) -> _Locator:
        assert selector == "img"
        return _Locator(self)


def _session_on(page: _Page) -> BrowserSession:
    session = BrowserSession(Throttle(0.0, sleep=lambda _: None))
    session._page = page  # 测试替换页面,跳过 Chromium 启动
    return session


def test_render_png_embeds_svg_and_screenshots_the_image() -> None:
    page = _Page()
    assert _session_on(page).render_png(SVG_PAGES[0], len(PNG_BYTES)) == PNG_BYTES
    assert base64.b64encode(SVG_PAGES[0]).decode("ascii") in page.html
    assert page.shots == [{"type": "png", "timeout": FETCH_TIMEOUT_MS}]


def test_render_png_rejects_output_over_cap() -> None:
    with pytest.raises(CgdlError) as info:
        _session_on(_Page()).render_png(SVG_PAGES[0], len(PNG_BYTES) - 1)
    assert info.value.code == DOWNLOAD_FAILED
