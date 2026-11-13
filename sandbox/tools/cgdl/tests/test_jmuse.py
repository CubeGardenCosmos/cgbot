"""token 推导、jmuse 响应解析、build 脚本 secret 提取的离线测试。"""

import hashlib

import pytest
from fixtures import BUILD_JS, BUILD_JS_LOOSE, FAKE_SECRET

from cgdl.errors import DOWNLOAD_FAILED, PARSE_FAILED, CgdlError
from cgdl.jmuse import find_token_secret, jmuse_url, make_token, parse_jmuse


def test_find_secret_in_build_script() -> None:
    assert find_token_secret(BUILD_JS) == FAKE_SECRET


def test_find_secret_absent() -> None:
    assert find_token_secret("var a = b + c;") is None


def test_make_token_matches_md5_prefix() -> None:
    expected = hashlib.md5(b"6969530mxl0zq", usedforsecurity=False).hexdigest()[:4]
    token = make_token(6969530, "mxl", 0, FAKE_SECRET)
    assert token == expected
    assert len(token) == 4


def test_jmuse_url_shape() -> None:
    assert jmuse_url(7, "img", 3) == "https://musescore.com/api/jmuse?id=7&index=3&type=img"


def test_parse_jmuse_returns_url() -> None:
    payload = {
        "result": "success",
        "status": 200,
        "error": None,
        "info": {"url": "https://s3w.musescore.com/a.mid"},
    }
    assert parse_jmuse(200, payload) == "https://s3w.musescore.com/a.mid"


def test_parse_jmuse_empty_url_means_absent() -> None:
    payload = {"result": "success", "status": 200, "error": None, "info": {"url": ""}}
    assert parse_jmuse(200, payload) is None


def test_parse_jmuse_404_means_absent() -> None:
    payload = {
        "result": "error",
        "status": 404,
        "error": {"name": "Not Found", "message": ""},
        "info": None,
    }
    assert parse_jmuse(404, payload) is None


def test_parse_jmuse_refusal_is_download_failed() -> None:
    with pytest.raises(CgdlError) as info:
        parse_jmuse(403, {"result": "error", "error": "forbidden"})
    assert info.value.code == DOWNLOAD_FAILED


@pytest.mark.parametrize("payload", [["list"], {"result": "success"}])
def test_parse_jmuse_malformed_is_parse_failed(payload: object) -> None:
    with pytest.raises(CgdlError) as info:
        parse_jmuse(200, payload)
    assert info.value.code == PARSE_FAILED


def test_find_secret_loose_rule_needs_md5_constants() -> None:
    assert find_token_secret(BUILD_JS_LOOSE) == FAKE_SECRET
    assert find_token_secret(BUILD_JS_LOOSE.replace("1732584193", "0")) is None


def test_loose_rule_ignored_without_md5_constants() -> None:
    script = f'function t(n){{return md5(n+"{FAKE_SECRET}").substr(0,4)}}'
    assert find_token_secret(script) is None
