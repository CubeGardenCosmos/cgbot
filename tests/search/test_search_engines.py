"""Search engines: HTML/JSON parsing, request shape, and stable error-code mapping (offline)."""

from __future__ import annotations

import aiohttp
import pytest
from search_fakes import (
    BAIDU_BLOCKED_HTML,
    BAIDU_HTML,
    BILI_JSON,
    BING_HTML,
    FakeTransport,
)

from cgbot.search.engines import (
    BaiduEngine,
    BilibiliEngine,
    BingEngine,
    EngineError,
    HttpReply,
    parse_baidu_web,
    parse_bilibili_web,
    parse_bing_web,
)
from cgbot.search.models import (
    E_BAIDU_BLOCKED,
    E_BILI_RISK,
    E_HTTP,
    E_NETWORK,
    E_NO_RESULT,
    E_PARSE,
    E_TIMEOUT,
)


def _ok(text: str) -> HttpReply:
    return HttpReply(status=200, text=text)


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def test_bing_parser_keeps_http_links_only_and_strips_markup() -> None:
    results = parse_bing_web(BING_HTML, 10)
    assert [r.url for r in results] == ["https://example.com/alpha", "https://example.com/beta"]
    assert results[0].title == "Alpha 术语"
    assert results[0].snippet == "Alpha snippet text"
    assert all(r.source == "bing" for r in results)


def test_bing_parser_respects_count() -> None:
    assert len(parse_bing_web(BING_HTML, 1)) == 1


def test_baidu_parser_uses_mu_cards_and_skips_cards_without_target() -> None:
    results = parse_baidu_web(BAIDU_HTML, 10)
    assert [r.url for r in results] == ["https://example.com/gamma", "https://example.com/delta"]
    assert results[0].snippet == "Gamma snippet"
    assert results[1].snippet == "Delta snippet"
    assert all(r.source == "baidu" for r in results)


def test_baidu_parser_raises_blocked_on_security_page() -> None:
    with pytest.raises(EngineError) as exc:
        parse_baidu_web(BAIDU_BLOCKED_HTML, 5)
    assert exc.value.code == E_BAIDU_BLOCKED


def test_bilibili_parser_builds_video_urls_and_meta_free_snippets() -> None:
    results = parse_bilibili_web(BILI_JSON, 10)
    assert [r.url for r in results] == [
        "https://www.bilibili.com/video/BV1xx411c7mD",
        "https://www.bilibili.com/video/BV1yy411c7mE",
    ]
    assert results[0].title == "Epsilon"
    assert results[0].snippet == "first line"
    assert all(r.source == "bilibili" for r in results)


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ('{"code":-412,"message":"risk"}', E_BILI_RISK),
        ('{"code":-352,"message":"risk"}', E_BILI_RISK),
        ('{"code":-400,"message":"bad"}', E_PARSE),
        ("not json", E_PARSE),
        ('["list"]', E_PARSE),
        ('{"code":0,"data":"oops"}', E_PARSE),
    ],
)
def test_bilibili_parser_error_codes(body: str, code: str) -> None:
    with pytest.raises(EngineError) as exc:
        parse_bilibili_web(body, 5)
    assert exc.value.code == code


# ---------------------------------------------------------------------------
# Bing engine
# ---------------------------------------------------------------------------


async def test_bing_engine_sends_query_count_and_freshness_filter() -> None:
    transport = FakeTransport(_ok(BING_HTML))
    engine = BingEngine(transport, timeout=5.0)
    results = await engine.search("量子计算", 3, freshness="week")
    assert len(results) == 2
    call = transport.calls[0]
    assert call["url"].endswith("/search")
    assert call["params"]["q"] == "量子计算"
    assert call["params"]["count"] == "3"
    assert call["params"]["filters"].startswith("ex1:")


async def test_bing_engine_ignores_unknown_freshness() -> None:
    transport = FakeTransport(_ok(BING_HTML))
    await BingEngine(transport, timeout=5.0).search("x", 3, freshness="century")
    assert "filters" not in transport.calls[0]["params"]


async def test_bing_engine_empty_page_is_no_result() -> None:
    transport = FakeTransport(_ok("<html></html>"))
    with pytest.raises(EngineError) as exc:
        await BingEngine(transport, timeout=5.0).search("x", 3)
    assert exc.value.code == E_NO_RESULT


async def test_bing_engine_maps_transport_failures() -> None:
    cases: list[tuple[HttpReply | BaseException, str]] = [
        (TimeoutError(), E_TIMEOUT),
        (aiohttp.ClientConnectionError("refused"), E_NETWORK),
        (HttpReply(status=500, text=""), E_HTTP),
    ]
    for item, code in cases:
        transport = FakeTransport(item)
        with pytest.raises(EngineError) as exc:
            await BingEngine(transport, timeout=5.0).search("x", 3)
        assert exc.value.code == code


async def test_engine_close_closes_its_transport() -> None:
    transport = FakeTransport()
    await BingEngine(transport, timeout=5.0).close()
    assert transport.closed


# ---------------------------------------------------------------------------
# Baidu engine
# ---------------------------------------------------------------------------


async def test_baidu_engine_warms_up_once_then_queries() -> None:
    transport = FakeTransport(_ok("<html>home</html>"), _ok(BAIDU_HTML), _ok(BAIDU_HTML))
    engine = BaiduEngine(transport, timeout=5.0)
    first = await engine.search("苹果", 5)
    second = await engine.search("香蕉", 5)
    assert [r.url for r in first] == ["https://example.com/gamma", "https://example.com/delta"]
    assert len(second) == 2
    assert len(transport.calls) == 3  # one warmup + two searches
    assert transport.calls[1]["params"]["wd"] == "苹果"
    assert transport.calls[2]["params"]["wd"] == "香蕉"


async def test_baidu_engine_continues_when_warmup_fails() -> None:
    transport = FakeTransport(aiohttp.ClientConnectionError("warm"), _ok(BAIDU_HTML))
    results = await BaiduEngine(transport, timeout=5.0).search("苹果", 5)
    assert len(results) == 2


async def test_baidu_engine_blocked_page_maps_to_blocked_code() -> None:
    transport = FakeTransport(_ok("<html>home</html>"), _ok(BAIDU_BLOCKED_HTML))
    with pytest.raises(EngineError) as exc:
        await BaiduEngine(transport, timeout=5.0).search("苹果", 5)
    assert exc.value.code == E_BAIDU_BLOCKED


async def test_baidu_engine_sends_configured_cookies_and_skips_warmup() -> None:
    transport = FakeTransport(_ok(BAIDU_HTML))
    await BaiduEngine(transport, timeout=5.0, cookies="BAIDUID=abc").search("苹果", 5)
    assert len(transport.calls) == 1
    assert transport.calls[0]["headers"]["Cookie"] == "BAIDUID=abc"


# ---------------------------------------------------------------------------
# Bilibili engine
# ---------------------------------------------------------------------------


async def test_bilibili_engine_warms_home_without_cookies() -> None:
    transport = FakeTransport(_ok("<html>home</html>"), _ok(BILI_JSON))
    results = await BilibiliEngine(transport, timeout=5.0).search("猫", 5)
    assert len(results) == 2
    assert transport.calls[0]["url"] == "https://www.bilibili.com/"
    assert transport.calls[1]["params"]["keyword"] == "猫"


async def test_bilibili_engine_with_cookies_skips_warmup_and_sends_cookie() -> None:
    transport = FakeTransport(_ok(BILI_JSON))
    await BilibiliEngine(transport, timeout=5.0, cookies="buvid3=xyz").search("猫", 5)
    assert len(transport.calls) == 1
    assert transport.calls[0]["headers"]["Cookie"] == "buvid3=xyz"


async def test_bilibili_engine_http_412_is_risk_code() -> None:
    transport = FakeTransport(HttpReply(status=412, text=""))
    engine = BilibiliEngine(transport, timeout=5.0, cookies="buvid3=xyz")
    with pytest.raises(EngineError) as exc:
        await engine.search("猫", 5)
    assert exc.value.code == E_BILI_RISK


async def test_bilibili_engine_empty_video_list_is_no_result() -> None:
    transport = FakeTransport(_ok('{"code":0,"data":{"result":[]}}'))
    with pytest.raises(EngineError) as exc:
        await BilibiliEngine(transport, timeout=5.0, cookies="buvid3=xyz").search("猫", 5)
    assert exc.value.code == E_NO_RESULT
