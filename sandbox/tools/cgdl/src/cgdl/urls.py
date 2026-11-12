"""乐谱 URL 与 ID 解析、搜索地址构造,以及主机白名单。

所有出网地址在请求前都必须经过 check_allowed 或 is_allowed_url。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote_plus, urlsplit

from cgdl.errors import BAD_INPUT, BAD_URL, CgdlError

# musescore.com 页面及其引用的资源域名。s3w.musescore.com 与 cdn.ustatik.com 由页面/jmuse 返回。
ALLOWED_HOSTS: frozenset[str] = frozenset(
    {
        "musescore.com",
        "www.musescore.com",
        "static.musescore.com",
        "s3w.musescore.com",
        "cdn.ustatik.com",
    }
)
_PAGE_HOSTS = frozenset({"musescore.com", "www.musescore.com"})
_SCORE_PATH = re.compile(r"/user/(\d{1,12})/scores/(\d{1,12})/?")
_BARE_ID = re.compile(r"\d{1,12}")
MAX_QUERY_CHARS = 200


@dataclass(frozen=True, slots=True)
class ScoreRef:
    """乐谱引用:规范 URL 中的 uid 与乐谱 id。"""

    score_id: int
    user_id: int

    @property
    def url(self) -> str:
        return f"https://musescore.com/user/{self.user_id}/scores/{self.score_id}"


def parse_score_ref(value: str) -> ScoreRef:
    """解析 https://musescore.com/user/<uid>/scores/<id> 形式的乐谱 URL。

    纯数字 id 无法解析:musescore.com/scores/<id> 返回 404,页面 URL 必须含 uid。
    """
    text = value.strip()
    if _BARE_ID.fullmatch(text):
        raise CgdlError(
            BAD_URL,
            "bare score ids cannot be resolved (musescore.com/scores/<id> returns 404); "
            "pass the full score URL https://musescore.com/user/<uid>/scores/<id>",
        )
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError as exc:
        raise CgdlError(BAD_URL, "malformed URL") from exc
    if parts.scheme != "https" or parts.hostname not in _PAGE_HOSTS or port not in (None, 443):
        raise CgdlError(BAD_URL, "expected an https://musescore.com score URL")
    if parts.username is not None or parts.password is not None:
        raise CgdlError(BAD_URL, "URLs with credentials are not accepted")
    match = _SCORE_PATH.fullmatch(parts.path)
    if match is None:
        raise CgdlError(BAD_URL, "URL path must be /user/<uid>/scores/<id>")
    return ScoreRef(score_id=int(match[2]), user_id=int(match[1]))


def is_allowed_url(url: str) -> bool:
    """https、白名单主机、无凭据、端口为 443 或缺省。"""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and parts.hostname in ALLOWED_HOSTS
        and parts.username is None
        and parts.password is None
        and port in (None, 443)
    )


def check_allowed(url: str, code: str) -> str:
    """不在白名单内则以 code 抛错。错误信息只含主机名,不含签名查询串。"""
    if is_allowed_url(url):
        return url
    raise CgdlError(code, f"refusing to contact host outside the allowlist: {host_of(url)}")


def host_of(url: str) -> str:
    try:
        return urlsplit(url).hostname or "?"
    except ValueError:
        return "?"


def search_url(query: str) -> str:
    text = " ".join(query.split())
    if not text or len(text) > MAX_QUERY_CHARS:
        raise CgdlError(BAD_INPUT, f"query must be 1-{MAX_QUERY_CHARS} characters")
    return f"https://musescore.com/sheetmusic?text={quote_plus(text)}"
