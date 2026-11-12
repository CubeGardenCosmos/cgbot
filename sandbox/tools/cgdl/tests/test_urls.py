"""URL 解析、白名单与搜索地址的离线测试。"""

import pytest

from cgdl.errors import BAD_INPUT, BAD_URL, DOWNLOAD_FAILED, CgdlError
from cgdl.urls import check_allowed, is_allowed_url, parse_score_ref, search_url

URL = "https://musescore.com/user/39593079/scores/6969530"


def test_parse_full_score_url() -> None:
    ref = parse_score_ref(URL)
    assert (ref.score_id, ref.user_id) == (6969530, 39593079)
    assert ref.url == URL


def test_parse_accepts_www_and_trailing_slash() -> None:
    ref = parse_score_ref("https://www.musescore.com/user/1/scores/2/")
    assert ref.url == "https://musescore.com/user/1/scores/2"


@pytest.mark.parametrize(
    "value",
    [
        "6969530",
        "  6969530  ",
        "http://musescore.com/user/1/scores/2",
        "https://evil.example/user/1/scores/2",
        "https://musescore.com/scores/6969530",
        "https://musescore.com/user/1/scores/abc",
        "https://user:pw@musescore.com/user/1/scores/2",
        "https://musescore.com:8443/user/1/scores/2",
        "not a url",
    ],
)
def test_parse_rejects_bad_urls(value: str) -> None:
    with pytest.raises(CgdlError) as info:
        parse_score_ref(value)
    assert info.value.code == BAD_URL


@pytest.mark.parametrize(
    "url",
    [
        "https://musescore.com/sheetmusic",
        "https://www.musescore.com/x",
        "https://static.musescore.com/x.js",
        "https://s3w.musescore.com/x.mid",
        "https://cdn.ustatik.com/x.png",
    ],
)
def test_allowlist_accepts_listed_hosts(url: str) -> None:
    assert is_allowed_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://musescore.com/x",
        "https://musescore.org/x",
        "https://musescore.com.evil.example/x",
        "https://u@musescore.com/x",
        "https://musescore.com:8443/x",
        "https://evil.example/?next=musescore.com",
    ],
)
def test_allowlist_rejects_others(url: str) -> None:
    assert not is_allowed_url(url)


def test_check_allowed_raises_with_code_and_no_query() -> None:
    with pytest.raises(CgdlError) as info:
        check_allowed("https://evil.example/f?sig=SECRET", DOWNLOAD_FAILED)
    assert info.value.code == DOWNLOAD_FAILED
    assert "SECRET" not in info.value.message
    assert "evil.example" in info.value.message


def test_search_url_encodes_and_collapses_spaces() -> None:
    assert (
        search_url("gymnopedie  No. 1") == "https://musescore.com/sheetmusic?text=gymnopedie+No.+1"
    )


@pytest.mark.parametrize("query", ["", "   ", "a" * 201])
def test_search_url_rejects_empty_or_long(query: str) -> None:
    with pytest.raises(CgdlError) as info:
        search_url(query)
    assert info.value.code == BAD_INPUT
