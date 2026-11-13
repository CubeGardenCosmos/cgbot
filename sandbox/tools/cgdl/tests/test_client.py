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
from cgdl.secretcache import SecretCache
from cgdl.urls import ScoreRef, search_url

REF = ScoreRef(score_id=6969530, user_id=39593079)
S3 = "https://s3w.musescore.com/musescore.scoredata/g/h/"


LEGACY = "9654,4e"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"rendered"


@pytest.fixture(autouse=True)
def cache_dir(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """每个测试使用独立的 secret 缓存目录,绝不读写真实的 ~/.cache/cgdl。"""
    directory = tmp_path_factory.mktemp("cgdl-cache")
    monkeypatch.setenv("CGDL_CACHE_DIR", str(directory))
    return directory


class FakeBrowser:
    """实现 client.PageBrowser 的假浏览器。
    jmuse 只接受由 accepted 中某个 secret 推导的令牌,否则 422。"""

    def __init__(
        self,
        *,
        html: str = SCORE_HTML,
        jmuse: dict[tuple[str, int], str] | None = None,
        files: dict[str, bytes] | None = None,
        accepted: tuple[str, ...] = (FAKE_SECRET,),
    ) -> None:
        self.html = html
        self.jmuse = jmuse or {}
        self.files = files or {}
        self.accepted = accepted
        self.started = False
        self.closed = False
        self.opened: list[str] = []
        self.open_waits: list[float] = []
        self.fetch_calls: list[tuple[str, dict[str, str]]] = []
        self.text_fetches: list[str] = []
        self.rendered: list[list[bytes]] = []
        self.png_inputs: list[bytes] = []

    def start(self) -> None:
        self.started = True

    def close(self) -> None:
        self.closed = True

    def open(self, url: str, challenge_wait_s: float) -> tuple[int | None, str]:
        self.opened.append(url)
        self.open_waits.append(challenge_wait_s)
        return 200, self.html

    def page_fetch(self, url: str, headers: dict[str, str]) -> tuple[int, str]:
        self.fetch_calls.append((url, headers))
        query = parse_qs(urlsplit(url).query)
        score_id, jtype, index = int(query["id"][0]), query["type"][0], int(query["index"][0])
        token = headers["authorization"]
        if all(make_token(score_id, jtype, index, secret) != token for secret in self.accepted):
            return 422, '{"result": "error", "error": "invalid token"}'
        return 200, jmuse_json(self.jmuse.get((jtype, index), ""))

    def script_sources(self) -> list[str]:
        return ["https://cdn.ustatik.com/other.js", BUILD_URL]

    def fetch_text(self, url: str, max_bytes: int) -> str:
        self.text_fetches.append(url)
        assert url == BUILD_URL
        return BUILD_JS

    def fetch_bytes(self, url: str, max_bytes: int) -> bytes:
        return self.files[url]

    def render_pdf(self, pages: list[bytes], max_bytes: int) -> bytes:
        self.rendered.append(pages)
        return b"%PDF-1.4 rendered"

    def render_png(self, svg: bytes, max_bytes: int) -> bytes:
        self.png_inputs.append(svg)
        return PNG_BYTES


def _plain(transport: httpx.MockTransport) -> PlainHttp:
    return PlainHttp(Throttle(0.0, sleep=lambda _: None), transport=transport)


def _score_only(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=SCORE_HTML.encode())


def _client(
    plain: PlainHttp, browser: FakeBrowser | None, sleeps: list[float] | None = None
) -> MuseScoreClient:
    def factory(_: Throttle) -> FakeBrowser:
        assert browser is not None, "browser must not be started in this test"
        return browser

    return MuseScoreClient(
        throttle=Throttle(0.0, sleep=lambda _: None),
        plain=plain,
        browser_factory=factory,
        sleep=(sleeps if sleeps is not None else []).append,
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
        def open(self, url: str, challenge_wait_s: float) -> tuple[int | None, str]:
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
    [
        ("mid", MID_BYTES),
        ("mxl", MXL_BYTES),
        ("mp3", MP3_BYTES),
        ("pdf", b"%PDF-1.4"),
        ("png", PNG_BYTES),
    ],
)
def test_check_format_accepts_valid_magic(kind: str, data: bytes) -> None:
    _check_format(kind, data)


@pytest.mark.parametrize("kind", ["mid", "mxl", "mp3", "pdf", "png"])
def test_check_format_rejects_html(kind: str) -> None:
    with pytest.raises(CgdlError) as info:
        _check_format(kind, b"<html>blocked</html>")
    assert info.value.code == DOWNLOAD_FAILED


def _tokens(browser: FakeBrowser) -> list[str]:
    return [headers["authorization"] for _, headers in browser.fetch_calls]


def test_get_discovers_secret_from_build_script_and_caches_it(
    tmp_path: Path, cache_dir: Path
) -> None:
    browser = FakeBrowser(
        jmuse={("mp3", 0): S3 + "score.mp3"},
        files={S3 + "score.mp3": MP3_BYTES},
    )
    with _client(_plain(httpx.MockTransport(_score_only)), browser) as client:
        client.get(REF, "mp3", tmp_path)
    assert browser.text_fetches == [BUILD_URL]
    assert SecretCache(cache_dir).load() == (BUILD_URL, FAKE_SECRET)


def test_cached_secret_is_used_without_rediscovery(tmp_path: Path, cache_dir: Path) -> None:
    SecretCache(cache_dir).store(BUILD_URL, FAKE_SECRET)
    browser = FakeBrowser(
        jmuse={("mp3", 0): S3 + "score.mp3"},
        files={S3 + "score.mp3": MP3_BYTES},
    )
    with _client(_plain(httpx.MockTransport(_score_only)), browser) as client:
        client.get(REF, "mp3", tmp_path)
    assert browser.text_fetches == []
    assert _tokens(browser) == [make_token(REF.score_id, "mp3", 0, FAKE_SECRET)]


def test_rejected_cached_secret_is_invalidated_and_literal_used(
    tmp_path: Path, cache_dir: Path
) -> None:
    SecretCache(cache_dir).store(BUILD_URL, "stale")
    browser = FakeBrowser(
        jmuse={("mp3", 0): S3 + "score.mp3"},
        files={S3 + "score.mp3": MP3_BYTES},
        accepted=(LEGACY,),
    )
    sleeps: list[float] = []
    with _client(_plain(httpx.MockTransport(_score_only)), browser, sleeps) as client:
        result = client.get(REF, "mp3", tmp_path)
    assert result["kind"] == "mp3"
    assert _tokens(browser) == [
        make_token(REF.score_id, "mp3", 0, "stale"),
        make_token(REF.score_id, "mp3", 0, LEGACY),
    ]
    assert sleeps == [2.0]
    assert browser.text_fetches == []
    assert SecretCache(cache_dir).load() is None


def test_literal_fallback_after_discovered_secret_is_rejected(
    tmp_path: Path, cache_dir: Path
) -> None:
    browser = FakeBrowser(
        jmuse={("mp3", 0): S3 + "score.mp3"},
        files={S3 + "score.mp3": MP3_BYTES},
        accepted=(LEGACY,),
    )
    sleeps: list[float] = []
    with _client(_plain(httpx.MockTransport(_score_only)), browser, sleeps) as client:
        client.get(REF, "mp3", tmp_path)
    assert browser.text_fetches == [BUILD_URL]
    assert _tokens(browser) == [
        make_token(REF.score_id, "mp3", 0, FAKE_SECRET),
        make_token(REF.score_id, "mp3", 0, LEGACY),
    ]
    assert sleeps == [2.0]
    assert SecretCache(cache_dir).load() is None


def test_jmuse_422_stops_after_three_tries(tmp_path: Path) -> None:
    browser = FakeBrowser(
        jmuse={("mp3", 0): S3 + "score.mp3"},
        files={S3 + "score.mp3": MP3_BYTES},
        accepted=(),
    )
    sleeps: list[float] = []
    with (
        _client(_plain(httpx.MockTransport(_score_only)), browser, sleeps) as client,
        pytest.raises(CgdlError) as info,
    ):
        client.get(REF, "mp3", tmp_path)
    assert info.value.code == DOWNLOAD_FAILED
    assert len(browser.fetch_calls) == 3
    assert sleeps == [2.0, 2.0]
    assert list(tmp_path.iterdir()) == []


def test_pdf_pages_keep_legacy_secret_after_fallback(tmp_path: Path) -> None:
    browser = FakeBrowser(
        jmuse={("img", 0): S3 + "p0.svg", ("img", 1): S3 + "p1.svg"},
        files={S3 + "p0.svg": SVG_BYTES, S3 + "p1.svg": SVG_BYTES + b"\n"},
        accepted=(LEGACY,),
    )
    sleeps: list[float] = []
    with _client(_plain(httpx.MockTransport(_score_only)), browser, sleeps) as client:
        result = client.get(REF, "pdf", tmp_path)
    assert result["pages"] == 2
    assert sleeps == [2.0]
    assert _tokens(browser)[1:] == [make_token(REF.score_id, "img", i, LEGACY) for i in range(3)]


def test_search_challenge_is_blocked_without_retry() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(
            403, content=CHALLENGE_HTML.encode(), headers={"cf-mitigated": "challenge"}
        )

    browser = FakeBrowser(html=CHALLENGE_HTML)
    with (
        _client(_plain(httpx.MockTransport(handler)), browser) as client,
        pytest.raises(CgdlError) as info,
    ):
        client.search("gymnopedie", 5)
    assert info.value.code == BLOCKED
    assert browser.open_waits == [15.0]
    assert requests == [search_url("gymnopedie")]


def test_get_challenge_waits_60_seconds(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403, content=CHALLENGE_HTML.encode(), headers={"cf-mitigated": "challenge"}
        )

    browser = FakeBrowser(html=CHALLENGE_HTML)
    with (
        _client(_plain(httpx.MockTransport(handler)), browser) as client,
        pytest.raises(CgdlError) as info,
    ):
        client.get(REF, "mid", tmp_path)
    assert info.value.code == BLOCKED
    assert browser.open_waits == [60.0]


def test_png_renders_requested_page(tmp_path: Path) -> None:
    browser = FakeBrowser(
        jmuse={("img", 1): S3 + "p1.svg"},
        files={S3 + "p1.svg": SVG_BYTES},
    )
    with _client(_plain(httpx.MockTransport(_score_only)), browser) as client:
        result = client.get(REF, "png", tmp_path, page=2)
    path = Path(str(result["path"]))
    assert result["kind"] == "png"
    assert result["bytes"] == len(PNG_BYTES)
    assert path.name.endswith("-p2.png")
    assert path.read_bytes() == PNG_BYTES
    assert browser.png_inputs == [SVG_BYTES]
    assert _tokens(browser) == [make_token(REF.score_id, "img", 1, FAKE_SECRET)]


def test_png_missing_page_is_not_found(tmp_path: Path) -> None:
    browser = FakeBrowser(
        jmuse={("img", 0): S3 + "p0.svg"},
        files={S3 + "p0.svg": SVG_BYTES},
    )
    with (
        _client(_plain(httpx.MockTransport(_score_only)), browser) as client,
        pytest.raises(CgdlError) as info,
    ):
        client.get(REF, "png", tmp_path, page=5)
    assert info.value.code == NOT_FOUND
    assert browser.png_inputs == []
    assert list(tmp_path.iterdir()) == []
