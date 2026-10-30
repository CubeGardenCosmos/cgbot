"""Offline fakes for the search package: scripted transport, fake engines, HTML/JSON fixtures."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from cgbot.search.engines import HttpReply
from cgbot.search.models import SearchResult

BING_HTML = """
<html><body><ol>
<li class="b_algo"><h2><a href="https://example.com/alpha">Alpha <b>术语</b></a></h2>
<div class="b_caption"><p>Alpha snippet text</p></div></li>
<li class="b_algo"><h2><a href="javascript:void(0)">Skipped</a></h2><p>no http link</p></li>
<li class="b_algo"><h2><a href="https://example.com/beta">Beta</a></h2>
<div class="b_caption"><p>Beta &amp; more</p></div></li>
</ol></body></html>
"""

BAIDU_HTML = """
<html><body>
<div class="result c-container new-pmd" mu="https://example.com/gamma"><h3><a>Gamma</a></h3>
<div class="c-abstract">Gamma snippet</div></div>
<div class="result c-container" id="ad"><span>no mu</span></div>
<div class="result c-container new-pmd" mu="https://example.com/delta"><h3><a>Delta</a></h3>
<div class="content-right">Delta snippet</div></div>
</body></html>
"""

BAIDU_BLOCKED_HTML = "<html><head><title>安全验证</title></head><body>wappass</body></html>"

BILI_JSON = (
    '{"code":0,"message":"0","data":{"result":['
    '{"bvid":"BV1xx411c7mD","title":"<em class=\\"keyword\\">Epsilon</em>",'
    '"description":"first\\nline","arcurl":"http://www.bilibili.com/video/BV1xx411c7mD"},'
    '{"bvid":"BV1yy411c7mE","title":"Zeta","description":"","arcurl":""}'
    "]}}"
)


class FakeTransport:
    """Scripted ``Transport``: each ``get`` pops the next item (an HttpReply or an exception)."""

    def __init__(
        self, *script: HttpReply | BaseException, default: HttpReply | None = None
    ) -> None:
        self._script = list(script)
        self._default = default
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    async def get(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None,
        headers: Mapping[str, str],
        timeout: float,  # noqa: ASYNC109 - mirrors the Transport protocol signature
    ) -> HttpReply:
        self.calls.append({"url": url, "params": dict(params or {}), "headers": dict(headers)})
        if self._script:
            item = self._script.pop(0)
        elif self._default is not None:
            item = self._default
        else:
            raise AssertionError(f"unscripted request: {url}")
        if isinstance(item, BaseException):
            raise item
        return item

    async def close(self) -> None:
        self.closed = True


class FakeEngine:
    """Scripted ``SearchEngine``: returns ``results`` or raises ``error``; optional delay."""

    def __init__(
        self,
        name: str,
        results: list[SearchResult] | None = None,
        *,
        error: BaseException | None = None,
        delay: float = 0.0,
    ) -> None:
        self.name = name
        self._results = results or []
        self._error = error
        self._delay = delay
        self.queries: list[tuple[str, int, str]] = []
        self.closed = False

    async def search(self, query: str, count: int, freshness: str = "") -> list[SearchResult]:
        self.queries.append((query, count, freshness))
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error is not None:
            raise self._error
        return list(self._results[:count])

    async def close(self) -> None:
        self.closed = True


def hit(
    title: str, url: str, snippet: str = "", source: str = "bing", meta: str = ""
) -> SearchResult:
    return SearchResult(title=title, url=url, snippet=snippet, source=source, meta=meta)
