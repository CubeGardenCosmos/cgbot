"""BrowserSession 在挑战通过后页面跳转期间读取内容的离线测试:假页面替代 Chromium。"""

from __future__ import annotations

from typing import Any

import pytest

from cgdl.browser import BrowserSession
from cgdl.errors import DOWNLOAD_FAILED, CgdlError
from cgdl.http import Throttle


class _NavigatingPage:
    """前 ``failures`` 次 content() 抛出 Playwright 在跳转期间的错误,之后返回 HTML。"""

    def __init__(self, failures: int, html: str = "<html>ok</html>") -> None:
        self.failures = failures
        self.html = html
        self.content_calls = 0
        self.settled = 0

    def content(self) -> str:
        self.content_calls += 1
        if self.content_calls <= self.failures:
            raise RuntimeError(
                "Page.content: Unable to retrieve content because the page is navigating"
            )
        return self.html

    def wait_for_load_state(self, state: str, timeout: float) -> None:
        assert state == "domcontentloaded"
        self.settled += 1


def _session(page: Any) -> BrowserSession:
    session = BrowserSession(Throttle(0.0, sleep=lambda _: None))
    session._page = page  # 测试替换页面,跳过 Chromium 启动
    return session


def test_content_is_retried_until_the_navigation_settles() -> None:
    page = _NavigatingPage(failures=2)

    html = _session(page)._settled_content(page)

    assert html == "<html>ok</html>"
    assert page.content_calls == 3
    assert page.settled == 2


def test_content_that_never_settles_is_reported_as_download_failed_not_a_traceback() -> None:
    page = _NavigatingPage(failures=99)

    with pytest.raises(CgdlError) as caught:
        _session(page)._settled_content(page)

    assert caught.value.code == DOWNLOAD_FAILED
    assert page.content_calls == 5
