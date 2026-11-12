"""HTTP 通道与限速器的离线测试(httpx.MockTransport,不触网)。"""

import httpx
import pytest

from cgdl.browser import pdf_document, proxy_settings
from cgdl.errors import DOWNLOAD_FAILED, CgdlError
from cgdl.http import PlainHttp, Throttle, blocked_status


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_throttle_sleeps_remaining_interval() -> None:
    fake = FakeClock()
    throttle = Throttle(1.0, clock=fake.clock, sleep=fake.sleep)
    throttle.wait()
    fake.now += 0.25
    throttle.wait()
    assert fake.sleeps == [pytest.approx(0.75)]


def test_throttle_no_sleep_after_interval() -> None:
    fake = FakeClock()
    throttle = Throttle(1.0, clock=fake.clock, sleep=fake.sleep)
    throttle.wait()
    fake.now += 5.0
    throttle.wait()
    assert fake.sleeps == []


def test_plain_get_returns_body_and_cf_header() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>ok</html>", headers={"cf-mitigated": "x"})

    plain = PlainHttp(Throttle(0.0, sleep=lambda _: None), transport=httpx.MockTransport(handler))
    fetched = plain.get("https://musescore.com/sheetmusic?text=x")
    assert fetched.status == 200
    assert fetched.html == "<html>ok</html>"
    assert fetched.cf_mitigated == "x"
    plain.close()


def test_plain_refuses_disallowed_host_without_request() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200)

    plain = PlainHttp(Throttle(0.0, sleep=lambda _: None), transport=httpx.MockTransport(handler))
    with pytest.raises(CgdlError) as info:
        plain.get("https://evil.example/x")
    assert info.value.code == DOWNLOAD_FAILED
    assert calls == []
    plain.close()


def test_plain_refuses_redirect_off_allowlist() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "musescore.com":
            return httpx.Response(302, headers={"location": "https://evil.example/landing"})
        return httpx.Response(200, content=b"x")

    plain = PlainHttp(Throttle(0.0, sleep=lambda _: None), transport=httpx.MockTransport(handler))
    with pytest.raises(CgdlError) as info:
        plain.get("https://musescore.com/a")
    assert info.value.code == DOWNLOAD_FAILED
    plain.close()


def test_plain_enforces_size_cap() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 10)

    plain = PlainHttp(
        Throttle(0.0, sleep=lambda _: None), transport=httpx.MockTransport(handler), max_bytes=5
    )
    with pytest.raises(CgdlError) as info:
        plain.get("https://musescore.com/a")
    assert info.value.code == DOWNLOAD_FAILED
    plain.close()


def test_plain_transport_error_is_download_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    plain = PlainHttp(Throttle(0.0, sleep=lambda _: None), transport=httpx.MockTransport(handler))
    with pytest.raises(CgdlError) as info:
        plain.get("https://musescore.com/a")
    assert info.value.code == DOWNLOAD_FAILED
    plain.close()


@pytest.mark.parametrize(
    ("status", "html", "cf", "expected"),
    [
        (403, "", None, True),
        (429, "", None, True),
        (503, "", None, True),
        (200, "<title>Just a moment...</title>", None, True),
        (200, "<title>Score</title>", "challenge", True),
        (200, "<title>Score</title>", None, False),
    ],
)
def test_blocked_status(status: int, html: str, cf: str | None, expected: bool) -> None:
    assert blocked_status(status, html, cf) is expected


def test_proxy_settings_splits_credentials() -> None:
    settings = proxy_settings("http://user%40x:p%3Ass@127.0.0.1:10808")
    assert settings == {
        "server": "http://127.0.0.1:10808",
        "username": "user@x",
        "password": "p:ss",
    }


def test_pdf_document_embeds_each_page_as_image() -> None:
    document = pdf_document([b"<svg/>", b"<svg/>"])
    assert document.count("<img ") == 2
    assert document.count("data:image/svg+xml;base64,") == 2
