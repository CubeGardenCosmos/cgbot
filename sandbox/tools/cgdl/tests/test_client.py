"""MuseScoreClient 编排的离线测试:假浏览器 + MockTransport,覆盖三种策略与各 kind。"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fixtures import (
    BUILD_JS,
    BUILD_URL,
    CHALLENGE_HTML,
    FAKE_SECRET,
    MID_BYTES,
    MP3_BYTES,
    MXL_BYTES,
    SCORE_HTML,
    SEARCH_HTML,
    SVG_BYTES,
    jmuse_json,
)

from cgdl.client import MAX_PAGES, MuseScoreClient, _check_format
from cgdl.errors import (
    BLOCKED,
    DOWNLOAD_FAILED,
    NOT_FOUND,
    PARSE_FAILED,
    UNSUPPORTED_KIND,
    CgdlError,
)
from cgdl.http import PlainHttp, Throttle
from cgdl.jmuse import make_token
from cgdl.urls import ScoreRef, search_url

REF = ScoreRef(score_id=6969530, user_id=39593079)
S3 = "https://s3w.musescore.com/musescore.scoredata/g/h/"


class FakeBrowser:
    """实现 client.PageBrowser 的假浏览器;按 (type, index) 返回 jmuse 结果。"""

    def __init__(
        self,
        *,
        html: str = SCORE_HTML,
        jmuse: dict[tuple[str, int], str] | None = None,
        files: dict[str, bytes] | None = None,
    ) -> None:
        self.html = html
        self.jmuse = jmuse or {}
        self.files = files or {}
        self.started = False
        self.closed = False
        self.opened: list[str] = []
        self.fetch_calls: list[tuple[str, dict[str, str]]] = []
        self.rendered: list[list[bytes]] = []

    def start(self) -> None:
        self.started = True

    def close(self) -> None:
        self.closed = True

    def open(self, url: str) -> tuple[int | None, str]:
        self.opened.append(url)
        return 200, self.html

    def page_fetch(self, url: str, headers: dict[str, str]) -> tuple[int, str]:
        self.fetch_calls.append((url, headers))
        query = parse_qs(urlsplit(url).query)
        key = (query["type"][0], int(query["index"][0]))
        return 200, jmuse_json(self.jmuse.get(key, ""))

    def script_sources(self) -> list[str]:
        return ["https://cdn.ustatik.com/other.js", BUILD_URL]

    def fetch_text(self, url: str, max_bytes: int) -> str:
        assert url == BUILD_URL
        return BUILD_JS

    def fetch_bytes(self, url: str, max_bytes: int) -> bytes:
        return self.files[url]

    def render_pdf(self, pages: list[bytes], max_bytes: int) -> bytes:
        self.rendered.append(pages)
        return b"%PDF-1.4 rendered"


def _plain(transport: httpx.MockTransport) -> PlainHttp:
    return PlainHttp(Throttle(0.0, sleep=lambda _: None), transport=transport)


def _score_only(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=SCORE_HTML.encode())


def _client(plain: PlainHttp, browser: FakeBrowser | None) -> MuseScoreClient:
    def factory(_: Throttle) -> FakeBrowser:
        assert browser is not None, "browser must not be started in this test"
        return browser

    return MuseScoreClient(
        throttle=Throttle(0.0, sleep=lambda _: None),
        plain=plain,
        browser_factory=factory,
    )


def test_search_plain_success_never_starts_browser() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == search_url("gymnopedie")
        return httpx.Response(200, content=SEARCH_HTML.encode())

    with _client(_plain(httpx.MockTransport(handler)), None) as client:
        hits = client.search("gymnopedie", 1)
    assert [hit.score_id for hit in hits] == [6969530]


def test_search_falls_back_to_browser_on_challenge() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403, content=CHALLENGE_HTML.encode(), headers={"cf-mitigated": "challenge"}
        )

    browser = FakeBrowser(html=SEARCH_HTML)
    with _client(_plain(httpx.MockTransport(handler)), browser) as client:
        hits = client.search("gymnopedie", 10)
    assert len(hits) == 2
    assert browser.started
    assert browser.opened == [search_url("gymnopedie")]
    assert browser.closed


def test_search_limit_applies() -> None:
    browser = FakeBrowser(html=SEARCH_HTML)
    plain = _plain(httpx.MockTransport(lambda request: httpx.Response(403)))
    with _client(plain, browser) as client:
        assert len(client.search("x", 1)) == 1


def test_search_unrecognised_page_is_parse_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html><title>Something else</title></html>")

    with (
        _client(_plain(httpx.MockTransport(handler)), None) as client,
        pytest.raises(CgdlError) as info,
    ):
        client.search("x", 5)
    assert info.value.code == PARSE_FAILED


def test_info_returns_metadata() -> None:
    with _client(_plain(httpx.MockTransport(_score_only)), None) as client:
        page = client.info(REF)
    assert page.title == "Gymnopédie No.1"
    assert page.price == 1.49


def test_get_mid_writes_verified_file(tmp_path: Path) -> None:
    dest_name = "Gymnopédie_No.1-6969530.mid"
    browser = FakeBrowser(
        jmuse={("midi", 0): S3 + "score.mid"},
        files={S3 + "score.mid": MID_BYTES},
    )
    with _client(_plain(httpx.MockTransport(_score_only)), browser) as client:
        result = client.get(REF, "mid", tmp_path)
    dest = tmp_path / dest_name
    assert result == {
        "kind": "mid",
        "path": str(dest),
        "bytes": len(MID_BYTES),
        "score_id": 6969530,
        "title": "Gymnopédie No.1",
        "url": REF.url,
    }
    assert dest.read_bytes() == MID_BYTES
    assert browser.opened == [REF.url]


def test_get_sends_token_derived_from_page_secret(tmp_path: Path) -> None:
    browser = FakeBrowser(
        jmuse={("mp3", 0): S3 + "score.mp3"},
        files={S3 + "score.mp3": MP3_BYTES},
    )
    with _client(_plain(httpx.MockTransport(_score_only)), browser) as client:
        client.get(REF, "mp3", tmp_path)
    ((url, headers),) = browser.fetch_calls
    assert url == "https://musescore.com/api/jmuse?id=6969530&index=0&type=mp3"
    assert headers == {"authorization": make_token(6969530, "mp3", 0, FAKE_SECRET)}


def test_get_mxl_without_file_is_not_found(tmp_path: Path) -> None:
    browser = FakeBrowser(jmuse={})
    with (
        _client(_plain(httpx.MockTransport(_score_only)), browser) as client,
        pytest.raises(CgdlError) as info,
    ):
        client.get(REF, "mxl", tmp_path)
    assert info.value.code == NOT_FOUND
    assert list(tmp_path.iterdir()) == []


def test_get_mxl_writes_zip_container(tmp_path: Path) -> None:
    browser = FakeBrowser(
        jmuse={("mxl", 0): S3 + "score.mxl"},
        files={S3 + "score.mxl": MXL_BYTES},
    )
    with _client(_plain(httpx.MockTransport(_score_only)), browser) as client:
        result = client.get(REF, "mxl", tmp_path)
    assert str(result["path"]).endswith("-6969530.mxl")


def test_get_pdf_walks_pages_and_renders(tmp_path: Path) -> None:
    browser = FakeBrowser(
        jmuse={("img", 0): S3 + "p0.svg", ("img", 1): S3 + "p1.svg"},
        files={S3 + "p0.svg": SVG_BYTES, S3 + "p1.svg": SVG_BYTES + b"\n"},
    )
    with _client(_plain(httpx.MockTransport(_score_only)), browser) as client:
        result = client.get(REF, "pdf", tmp_path)
    assert result["pages"] == 2
    assert str(result["path"]).endswith("-6969530.pdf")
    assert browser.rendered == [[SVG_BYTES, SVG_BYTES + b"\n"]]
    assert Path(str(result["path"])).read_bytes().startswith(b"%PDF")


def test_get_pdf_stops_at_page_cap(tmp_path: Path) -> None:
    browser = FakeBrowser(
        jmuse={("img", i): S3 + f"p{i}.svg" for i in range(MAX_PAGES)},
        files={S3 + f"p{i}.svg": SVG_BYTES for i in range(MAX_PAGES)},
    )
    with (
        _client(_plain(httpx.MockTransport(_score_only)), browser) as client,
        pytest.raises(CgdlError) as info,
    ):
        client.get(REF, "pdf", tmp_path)
    assert info.value.code == DOWNLOAD_FAILED
    assert list(tmp_path.iterdir()) == []


def test_get_rejects_unknown_kind(tmp_path: Path) -> None:
    with (
        _client(_plain(httpx.MockTransport(_score_only)), None) as client,
        pytest.raises(CgdlError) as info,
    ):
        client.get(REF, "gif", tmp_path)
    assert info.value.code == UNSUPPORTED_KIND


def test_get_rejects_wrong_magic_and_writes_nothing(tmp_path: Path) -> None:
    browser = FakeBrowser(
        jmuse={("midi", 0): S3 + "score.mid"},
        files={S3 + "score.mid": b"<html>not midi</html>"},
    )
    with (
        _client(_plain(httpx.MockTransport(_score_only)), browser) as client,
        pytest.raises(CgdlError) as info,
    ):
        client.get(REF, "mid", tmp_path)
    assert info.value.code == DOWNLOAD_FAILED
    assert list(tmp_path.iterdir()) == []


def test_get_blocked_when_challenge_never_clears(tmp_path: Path) -> None:
    class Blocked(FakeBrowser):
        def open(self, url: str) -> tuple[int | None, str]:
            raise CgdlError(BLOCKED, "Cloudflare challenge did not clear within 60 s")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403, content=CHALLENGE_HTML.encode(), headers={"cf-mitigated": "challenge"}
        )

    with (
        _client(_plain(httpx.MockTransport(handler)), Blocked()) as client,
        pytest.raises(CgdlError) as info,
    ):
        client.get(REF, "mid", tmp_path)
    assert info.value.code == BLOCKED


@pytest.mark.parametrize(
    ("kind", "data"),
    [("mid", MID_BYTES), ("mxl", MXL_BYTES), ("mp3", MP3_BYTES), ("pdf", b"%PDF-1.4")],
)
def test_check_format_accepts_valid_magic(kind: str, data: bytes) -> None:
    _check_format(kind, data)


@pytest.mark.parametrize("kind", ["mid", "mxl", "mp3", "pdf"])
def test_check_format_rejects_html(kind: str) -> None:
    with pytest.raises(CgdlError) as info:
        _check_format(kind, b"<html>blocked</html>")
    assert info.value.code == DOWNLOAD_FAILED
