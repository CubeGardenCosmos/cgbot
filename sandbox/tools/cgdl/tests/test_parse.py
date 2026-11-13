"""HTML 解析离线测试:搜索结果(数据区与卡片锚点)、乐谱元数据、挑战与 404 识别。"""

import pytest
from fixtures import (
    CHALLENGE_HTML,
    NOT_FOUND_HTML,
    SCORE_HTML,
    SEARCH_HTML,
    SEARCH_STORE_HTML,
    search_store_html,
)

from cgdl.errors import PARSE_FAILED, CgdlError
from cgdl.parse import (
    is_challenge,
    is_not_found_page,
    is_search_page,
    page_title,
    parse_score_page,
    parse_search_html,
)


def test_parse_search_cards_from_anchors() -> None:
    hits = parse_search_html(SEARCH_HTML)
    assert [hit.score_id for hit in hits] == [6969530, 222]
    first = hits[0]
    assert first.title == "Gymnopedie No. 1 - Erik Satie (Piano Solo)"
    assert first.url == "https://musescore.com/user/39593079/scores/6969530"
    assert first.composer is None
    assert hits[1].title == "Gymnopedie No. 3 - Erik Satie"


def test_parse_search_reads_store_attribute_and_drops_official() -> None:
    hits = parse_search_html(SEARCH_STORE_HTML)
    assert len(hits) == 1
    assert hits[0].to_dict() == {
        "score_id": 5,
        "url": "https://musescore.com/user/9/scores/5",
        "title": "Store Title",
        "composer": "Store Composer",
        "instruments": ["Piano"],
        "pages": 3,
        "rating": 4.5,
        "rating_count": 2,
        "duration": None,
        "license": "公有领域",
    }


def test_parse_search_empty_results_give_no_hits() -> None:
    html = search_store_html([])
    assert parse_search_html(html) == []
    assert is_search_page(html)


def test_parse_search_without_data_or_cards_is_parse_failed() -> None:
    html = "<html><head><title>Search result for 'zz' | MuseScore</title></head></html>"
    with pytest.raises(CgdlError) as info:
        parse_search_html(html)
    assert info.value.code == PARSE_FAILED


def test_parse_score_page_fields() -> None:
    page = parse_score_page(SCORE_HTML, 6969530)
    assert page.to_dict() == {
        "id": 6969530,
        "url": "https://musescore.com/user/39593079/scores/6969530",
        "title": "Gymnopédie No.1",
        "composer": "Erik Satie",
        "uploader": "Hal Leonard",
        "key": "D major, B minor",
        "published": "2016-01-01",
        "modified": "2026-01-01",
        "price": 1.49,
        "currency": "USD",
        "thumbnail": "https://cdn.ustatik.com/x.png",
    }


def test_parse_score_page_rejects_other_id() -> None:
    with pytest.raises(CgdlError) as info:
        parse_score_page(SCORE_HTML, 1)
    assert info.value.code == PARSE_FAILED


def test_parse_score_page_without_metadata_fails() -> None:
    with pytest.raises(CgdlError) as info:
        parse_score_page("<html><head></head></html>", 6969530)
    assert info.value.code == PARSE_FAILED


def test_challenge_detection() -> None:
    assert is_challenge(CHALLENGE_HTML, None)
    assert is_challenge(SEARCH_HTML, "challenge")
    assert not is_challenge(SEARCH_HTML, None)
    assert page_title(CHALLENGE_HTML) == "Just a moment..."


def test_not_found_detection() -> None:
    assert is_not_found_page(NOT_FOUND_HTML)
    assert not is_not_found_page(SEARCH_HTML)
