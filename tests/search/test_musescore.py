"""MuseScore 搜索与第 1 页预览:规范化、官方过滤、模糊排序、拦截与发送(假传输层,离线)。"""

from __future__ import annotations

import base64
import html as html_lib
import json
from typing import Any

import httpx
import pytest

from cgbot.agent.tools import ToolContext
from cgbot.config.settings import SearchConfig
from cgbot.model import Channel, OutboundMessage, SendResult
from cgbot.search import SearchService, search_tools
from cgbot.search.musescore import (
    E_BAD_REF,
    E_BLOCKED,
    E_NO_PREVIEW,
    E_NOT_FOUND,
    E_PARSE,
    E_REFUSED,
    E_TOO_LARGE,
    MAX_HTML_BYTES,
    MAX_IMAGE_BYTES,
    PNG_MAGIC,
    MuseScoreClient,
    MuseScoreError,
    ScoreHit,
    ScoreRef,
    format_hits,
    is_challenge_html,
    is_official,
    musescore_preview_tool,
    musescore_search_tool,
    normalise,
    page_score_id,
    parse_score_ref,
    parse_search_html,
    rank_hits,
    resolve_instrument,
    search_variants,
)

PNG = PNG_MAGIC + b"\x00" * 32
PAGE_URL = "https://musescore.com/user/1001/scores/101"
PNG_URL = "https://cdn.ustatik.com/musescore/scoredata/g/abcdef12/score_0.png@500x660?t=1"
HTML = {"content-type": "text/html; charset=utf-8"}


def _node(score_id: int, title: str, **extra: Any) -> dict[str, Any]:
    node: dict[str, Any] = {
        "id": score_id,
        "href": f"https://musescore.com/user/1001/scores/{score_id}",
        "title": title,
        "composer_name": "Frédéric Chopin",
        "instruments": [{"name": "Piano"}],
        "pages_count": 3,
        "rating": {"rating": 4.5, "count": 12},
        "duration": "3:20",
        "license": "CC BY-SA",
    }
    node.update(extra)
    return node


def _results_page(*nodes: dict[str, Any]) -> str:
    """搜索页:数据区属性值为 HTML 转义的 JSON(与 musescore.com 结构一致)。"""
    payload = {"store": {"page": {"data": {"scores": list(nodes)}}}}
    attr = html_lib.escape(json.dumps(payload, ensure_ascii=False), quote=True)
    body = f'<div data-props="{attr}"></div>'
    return f"<html><head><title>Sheet music</title></head><body>{body}</body></html>"


def _page_html(score_id: int, png_url: str | None = PNG_URL) -> str:
    og = f'<meta property="og:url" content="https://musescore.com/user/1001/scores/{score_id}">'
    img = f'<script>var p = "{png_url}";</script>' if png_url else ""
    return f"<html><head>{og}<title>Nocturne</title></head><body>{img}</body></html>"


def _client(handler: Any) -> MuseScoreClient:
    return MuseScoreClient(transport=httpx.MockTransport(handler), interval=0)


class _Recorder:
    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []

    async def __call__(self, message: OutboundMessage) -> SendResult:
        self.sent.append(message)
        return SendResult(ok=True)


def _ctx(sender: _Recorder) -> ToolContext:
    channel = Channel(kind="qq", account="1001")
    return ToolContext(
        user_id="1001",
        session_id="private:1001",
        source_channel=channel,
        source_message_id="m1",
        sender=sender,
        channels=(channel,),
    )


def _hit(score_id: int, title: str, composer: str | None = "Frédéric Chopin") -> ScoreHit:
    return ScoreHit(
        score_id=score_id,
        url=f"https://musescore.com/user/1001/scores/{score_id}",
        title=title,
        composer=composer,
        instruments=("Piano",),
        pages=2,
        rating=None,
        rating_count=None,
        duration=None,
        license="CC BY-SA",
    )


# ---------------------------------------------------------------------------
# 纯函数:规范化、搜索变体、乐器映射
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Nocturne Op.9 No.2", "nocturne op 9 no 2"),
        ("Chopín  —  Études!", "chopin etudes"),
        ("op9", "op 9"),
        ("钢琴 Sonata", "钢琴 sonata"),
    ],
)
def test_normalise_folds_case_accents_and_punctuation(text: str, expected: str) -> None:
    assert normalise(text) == expected


def test_search_variants_adds_normalised_form_only_when_different() -> None:
    assert search_variants(["nocturn op9"]) == ["nocturn op9", "nocturn op 9"]
    assert search_variants(["Chopin"]) == ["Chopin"]
    assert len(search_variants(["Chopin", "Nocturne", "op9"])) <= 2


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("钢琴", (2, None)),
        (" Violin ", (68, None)),
        ("bass guitar", (73, None)),
        ("小提琴", (68, None)),
        ("sitar", (None, "sitar")),
        ("", (None, None)),
    ],
)
def test_resolve_instrument_maps_aliases_and_keeps_unknown_as_keyword(
    text: str, expected: tuple[int | None, str | None]
) -> None:
    assert resolve_instrument(text) == expected


@pytest.mark.parametrize(
    "node",
    [
        {"is_official": True},
        {"isOfficial": True},
        {"official": True},
        {"publisher": {"name": "Edition"}},
        {"user": {"is_publisher": True}},
        {"badges": ["Official Score"]},
        {"labels": ["official"]},
    ],
)
def test_is_official_flags_every_official_marker(node: dict[str, Any]) -> None:
    assert is_official(node)


def test_is_official_leaves_community_scores_alone() -> None:
    assert not is_official({"id": 1, "user": {"is_publisher": False}, "badges": []})


# ---------------------------------------------------------------------------
# 页面解析:官方过滤、宿主与 id 校验、挑战识别
# ---------------------------------------------------------------------------


def test_official_scores_are_dropped_and_counted() -> None:
    page = _results_page(
        _node(101, "Nocturne Op. 9 No. 2"),
        _node(102, "Nocturne (official)", is_official=True),
        _node(103, "Nocturne publisher", publisher={"name": "Edition"}),
        _node(104, "Nocturne badge", badges=["Official Score"]),
    )
    hits, dropped = parse_search_html(page)
    assert [hit.score_id for hit in hits] == [101]
    assert dropped == 3


def test_hits_must_point_at_musescore_score_pages() -> None:
    page = _results_page(
        _node(201, "Foreign", href="https://evil.example/user/1/scores/201"),
        _node(202, "Mismatch", href="https://musescore.com/user/1/scores/999"),
        _node(203, "Good"),
    )
    hits, dropped = parse_search_html(page)
    assert [hit.score_id for hit in hits] == [203]
    assert dropped == 0


def test_search_page_without_result_data_is_parse_error() -> None:
    with pytest.raises(MuseScoreError) as info:
        parse_search_html("<html><body>no data</body></html>")
    assert info.value.code == E_PARSE


def test_challenge_titles_are_detected() -> None:
    assert is_challenge_html(b"<html><title>Just a moment...</title></html>")
    assert is_challenge_html(b"<title>Attention Required! | Cloudflare</title>")
    assert not is_challenge_html(b"<title>Nocturne</title>")


def test_page_without_og_url_is_parse_error() -> None:
    with pytest.raises(MuseScoreError) as info:
        page_score_id("<html></html>")
    assert info.value.code == E_PARSE


@pytest.mark.parametrize(
    "value",
    [
        "http://musescore.com/user/1001/scores/101",
        "https://evil.example/user/1/scores/101",
        "https://musescore.com/scores/101",
        "not a score",
    ],
)
def test_parse_score_ref_rejects_foreign_or_malformed_refs(value: str) -> None:
    with pytest.raises(MuseScoreError) as info:
        parse_score_ref(value)
    assert info.value.code == E_BAD_REF


def test_parse_score_ref_accepts_bare_id_and_full_url() -> None:
    assert parse_score_ref("101") == ScoreRef(101, None)
    assert parse_score_ref(PAGE_URL) == ScoreRef(101, PAGE_URL)


# ---------------------------------------------------------------------------
# 模糊排序与格式化
# ---------------------------------------------------------------------------


def test_rank_tolerates_typos_and_drops_irrelevant_hits() -> None:
    hits = [
        _hit(1, "Ballade No. 1"),
        _hit(2, "Nocturne Op. 9 No. 2"),
        _hit(3, "Nocturne in E-flat"),
    ]
    ranked = rank_hits(hits, ["Nocturnee op9"], limit=5)
    assert [hit.score_id for hit in ranked] == [2, 3]
    assert 1 not in [hit.score_id for hit in ranked]


def test_rank_respects_limit() -> None:
    hits = [_hit(i, f"Nocturne {i}") for i in range(1, 6)]
    assert len(rank_hits(hits, ["nocturne"], limit=2)) == 2


def test_format_hits_lists_fields_and_states_official_exclusion() -> None:
    text = format_hits([_hit(101, "Nocturne Op. 9 No. 2")], unmapped_instrument="sitar")
    assert "乐谱 id 101" in text
    assert "https://musescore.com/user/1001/scores/101" in text
    assert "已排除官方乐谱" in text
    assert "未识别的乐器「sitar」" in text
    assert format_hits([], unmapped_instrument=None) == (
        "没有找到符合条件的社区乐谱(已排除官方乐谱)。"
    )


# ---------------------------------------------------------------------------
# 客户端:搜索请求参数、拦截、重定向白名单
# ---------------------------------------------------------------------------


async def test_search_forces_non_official_and_passes_instrument() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            text=_results_page(
                _node(101, "Nocturne Op. 9 No. 2"),
                _node(102, "Nocturne Op. 9 (Editorial)", is_official=True),
            ),
            headers=HTML,
        )

    client = _client(handler)
    hits = await client.search(["nocturn op9"], ["nocturn op9"], instrument_id=2, limit=5)

    assert [hit.score_id for hit in hits] == [101]
    assert len(requests) == 2  # 原文与规范化形式各一次
    assert {request.url.params["text"] for request in requests} == {"nocturn op9", "nocturn op 9"}
    for request in requests:
        assert request.url.host == "musescore.com"
        assert request.url.params["type"] == "non-official"
        assert request.url.params["instrument"] == "2"


async def test_resolve_returns_url_for_ids_seen_in_search() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_results_page(_node(101, "Nocturne")), headers=HTML)

    client = _client(handler)
    await client.search(["nocturne"], ["nocturne"], instrument_id=None, limit=5)
    assert client.resolve(ScoreRef(101, None)) == (PAGE_URL, "Nocturne")
    with pytest.raises(MuseScoreError) as info:
        client.resolve(ScoreRef(999, None))
    assert info.value.code == E_BAD_REF


@pytest.mark.parametrize(
    ("status", "text", "headers"),
    [
        (403, "forbidden", {}),
        (200, "<title>Just a moment...</title>", {"content-type": "text/html"}),
        (200, "ok", {"cf-mitigated": "challenge"}),
    ],
)
async def test_cloudflare_challenge_is_reported_as_blocked(
    status: int, text: str, headers: dict[str, str]
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=text, headers=headers)

    with pytest.raises(MuseScoreError) as info:
        await _client(handler).search(["nocturne"], ["nocturne"], instrument_id=None, limit=5)
    assert info.value.code == E_BLOCKED


async def test_not_found_and_oversize_responses() -> None:
    def not_found(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="missing")

    with pytest.raises(MuseScoreError) as info:
        await _client(not_found).search(["nocturne"], ["nocturne"], instrument_id=None, limit=5)
    assert info.value.code == E_NOT_FOUND

    def oversize(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (MAX_HTML_BYTES + 1), headers=HTML)

    with pytest.raises(MuseScoreError) as info:
        await _client(oversize).search(["nocturne"], ["nocturne"], instrument_id=None, limit=5)
    assert info.value.code == E_TOO_LARGE


async def test_redirect_off_allowlist_is_refused_before_contact() -> None:
    contacted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        contacted.append(request.url.host)
        if request.url.host == "evil.example":
            return httpx.Response(200, text="x", headers=HTML)
        return httpx.Response(302, headers={"location": "https://evil.example/page"})

    with pytest.raises(MuseScoreError) as info:
        await _client(handler).search(["nocturne"], ["nocturne"], instrument_id=None, limit=5)
    assert info.value.code == E_REFUSED
    assert "evil.example" not in contacted


# ---------------------------------------------------------------------------
# 工具层:参数校验、搜索、预览发送
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"query": "x", "url": "y"}, "unknown argument"),
        ({"instrument": "piano"}, "至少提供一个"),
        ({"query": "x", "limit": 11}, "limit must be"),
        ({"query": "x", "limit": True}, "limit must be"),
        ({"query": 3}, "must be a string"),
        ({"query": "x" * 101}, "at most"),
    ],
)
async def test_search_tool_rejects_bad_arguments(args: dict[str, Any], message: str) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_results_page(), headers=HTML)

    result = await musescore_search_tool(_client(handler)).handler(_ctx(_Recorder()), args)
    assert result.is_error is True
    assert message in result.output


async def test_search_tool_maps_instrument_and_keeps_unknown_as_keyword() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, text=_results_page(_node(101, "Nocturne Op. 9 No. 2")), headers=HTML
        )

    spec = musescore_search_tool(_client(handler))
    result = await spec.handler(_ctx(_Recorder()), {"composer": "Chopin", "instrument": "钢琴"})
    assert result.is_error is False
    assert requests[-1].url.params["instrument"] == "2"
    assert requests[-1].url.params["text"] == "Chopin"

    result = await spec.handler(_ctx(_Recorder()), {"query": "nocturne", "instrument": "sitar"})
    assert result.is_error is False
    assert "instrument" not in requests[-1].url.params
    assert requests[-1].url.params["text"] == "nocturne sitar"
    assert "未识别的乐器「sitar」" in result.output


def _site(request: httpx.Request) -> httpx.Response:
    if request.url.host == "cdn.ustatik.com":
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
    if request.url.path == "/sheetmusic":
        return httpx.Response(
            200, text=_results_page(_node(101, "Nocturne Op. 9 No. 2")), headers=HTML
        )
    return httpx.Response(200, text=_page_html(101), headers=HTML)


async def test_preview_sends_first_page_png_to_requesting_user_only() -> None:
    sender = _Recorder()
    result = await musescore_preview_tool(_client(_site)).handler(_ctx(sender), {"score": PAGE_URL})

    assert result.is_error is False
    assert "第 1 页" in result.output
    (message,) = sender.sent
    assert message.scope == "private"
    assert message.target_id == "1001"
    (segment,) = message.segments
    assert segment.type == "image"
    file_ref = str(segment.data["file"])
    assert base64.b64decode(file_ref.removeprefix("base64://")) == PNG


async def test_preview_rejects_identity_arguments_and_unknown_bare_ids() -> None:
    sender = _Recorder()
    spec = musescore_preview_tool(_client(_site))

    result = await spec.handler(_ctx(sender), {"score": PAGE_URL, "target": "2002"})
    assert result.is_error is True
    assert "unknown argument" in result.output

    result = await spec.handler(_ctx(sender), {"score": "101"})
    assert result.is_error is True
    assert E_BAD_REF in result.output
    assert sender.sent == []


async def test_bare_id_preview_works_after_search_in_same_process() -> None:
    client = _client(_site)
    sender = _Recorder()
    await musescore_search_tool(client).handler(_ctx(sender), {"query": "nocturne"})
    result = await musescore_preview_tool(client).handler(_ctx(sender), {"score": "101"})
    assert result.is_error is False
    assert "《Nocturne Op. 9 No. 2》" in result.output
    assert len(sender.sent) == 1


async def test_preview_refuses_image_redirect_off_allowlist() -> None:
    contacted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        contacted.append(request.url.host)
        if request.url.host == "cdn.ustatik.com":
            return httpx.Response(302, headers={"location": "https://evil.example/a.png"})
        if request.url.host == "evil.example":
            return httpx.Response(200, content=PNG)
        return httpx.Response(200, text=_page_html(101), headers=HTML)

    sender = _Recorder()
    result = await musescore_preview_tool(_client(handler)).handler(
        _ctx(sender), {"score": PAGE_URL}
    )
    assert result.is_error is True
    assert E_REFUSED in result.output
    assert "evil.example" not in contacted
    assert sender.sent == []


async def test_preview_reports_page_mismatch_and_missing_image() -> None:
    def mismatch(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_page_html(999), headers=HTML)

    def no_image(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_page_html(101, None), headers=HTML)

    for site, code in ((mismatch, E_NOT_FOUND), (no_image, E_NO_PREVIEW)):
        sender = _Recorder()
        result = await musescore_preview_tool(_client(site)).handler(
            _ctx(sender), {"score": PAGE_URL}
        )
        assert result.is_error is True
        assert code in result.output
        assert sender.sent == []


async def test_preview_rejects_non_png_and_oversize_images() -> None:
    def svg_site(request: httpx.Request) -> httpx.Response:
        if request.url.host == "cdn.ustatik.com":
            return httpx.Response(200, content=b"<svg/>")
        return httpx.Response(200, text=_page_html(101), headers=HTML)

    result = await musescore_preview_tool(_client(svg_site)).handler(
        _ctx(_Recorder()), {"score": PAGE_URL}
    )
    assert result.is_error is True
    assert E_NO_PREVIEW in result.output

    def big_site(request: httpx.Request) -> httpx.Response:
        if request.url.host == "cdn.ustatik.com":
            return httpx.Response(200, content=PNG_MAGIC + b"0" * MAX_IMAGE_BYTES)
        return httpx.Response(200, text=_page_html(101), headers=HTML)

    result = await musescore_preview_tool(_client(big_site)).handler(
        _ctx(_Recorder()), {"score": PAGE_URL}
    )
    assert result.is_error is True
    assert E_TOO_LARGE in result.output


# ---------------------------------------------------------------------------
# 注册:仅在提供 musescore 客户端时出现在 search_tools 中
# ---------------------------------------------------------------------------


def test_musescore_tools_registered_only_when_client_supplied() -> None:
    service = SearchService(
        SearchConfig(engines=("bing",), timeout_seconds=5.0, default_count=5, max_count=10),
        engines={},
    )
    assert "musescore_search" not in [spec.name for spec in search_tools(service)]

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_results_page(), headers=HTML)

    names = [spec.name for spec in search_tools(service, musescore_client=_client(handler))]
    assert names[-2:] == ["musescore_search", "musescore_preview"]
