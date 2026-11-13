r"""/api/jmuse 的 token 推导、构建脚本发现与响应解析。

token 与页面脚本一致:md5(id + type + index + secret) 的前 4 位十六进制。
secret 不写死在代码里:从页面引用的 musescore 构建脚本(页面 <link>/<script> 与运行时 script 列表)
中提取。
提取规则与 ingui-musescore-downloader 的 SECRET_RE 一致:`\w+\w+\w+"<secret>").substr(0, 4)`。
另有一条宽松规则 `"<secret>").substr(0,4)`,仅用于同时含 MD5 初始化常量的脚本(LibreScore 的做法)。
两者都不命中时返回 None,由调用方报告失败,不使用硬编码回退值。
"""

from __future__ import annotations

import hashlib
import html as html_lib
import re
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from cgdl.errors import DOWNLOAD_FAILED, PARSE_FAILED, CgdlError
from cgdl.urls import is_allowed_url

MAX_BUILD_SCRIPTS = 12
# ingui BUILD_URL_RE:/static/public/build/<路径>/<数字>/<数字>.<名字>.js
_BUILD_URL_RE = re.compile(r"^https://musescore\.com/static/public/build/[\w/]+/\d+/\d+\.\w+\.js$")
# ingui / LibreScore 共用的 MD5 初始化常量;宽松规则只作用于含这些常量的脚本。
MD5_INIT_CONSTANTS = ("1732584193", "-271733879", "-1732584194", "271733878")
_STRICT_SECRET_RE = re.compile(r'\w\+\w\+\w\+"([^"\\]{1,64})"\)\.substr\(0,\s*4\)')
_LOOSE_SECRET_RE = re.compile(r'"([^"\\]{1,64})"\)\.substr\(0,\s*4\)')
_TAG_RE = re.compile(r"<(?:link|script)\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r"""\b(?:href|src)\s*=\s*["']([^"']+)["']""", re.IGNORECASE)


def build_script_url(raw: str) -> str | None:
    """相对或绝对地址 -> 允许的构建脚本 URL(去查询串与片段);不是构建脚本则返回 None。"""
    absolute = urljoin("https://musescore.com/", html_lib.unescape(raw.strip()))
    if not is_allowed_url(absolute):
        return None
    parts = urlsplit(absolute)
    clean = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    return clean if _BUILD_URL_RE.match(clean) else None


def find_build_script_urls(page_html: str) -> list[str]:
    """页面 <link>/<script> 引用的构建脚本,按文档顺序去重。"""
    urls: list[str] = []
    for tag in _TAG_RE.finditer(page_html):
        for attr in _ATTR_RE.finditer(tag.group(0)):
            url = build_script_url(attr[1])
            if url is not None and url not in urls:
                urls.append(url)
    return urls


def candidate_build_urls(page_html: str, live_sources: Iterable[str]) -> list[str]:
    """页面 HTML 引用优先,其后是运行时 script 列表;合并去重并限制数量。"""
    urls = find_build_script_urls(page_html)
    for source in live_sources:
        url = build_script_url(source)
        if url is not None and url not in urls:
            urls.append(url)
    return urls[:MAX_BUILD_SCRIPTS]


def find_token_secret(script_text: str) -> str | None:
    """从单个构建脚本提取 secret;无匹配返回 None。"""
    strict = _STRICT_SECRET_RE.search(script_text)
    if strict is not None:
        return strict[1]
    if all(constant in script_text for constant in MD5_INIT_CONSTANTS):
        loose = _LOOSE_SECRET_RE.search(script_text)
        if loose is not None:
            return loose[1]
    return None


def discover_token_secret(
    build_urls: Iterable[str], fetch_text: Callable[[str], str]
) -> tuple[str, str] | None:
    """按顺序惰性获取构建脚本,返回首个命中的 (脚本 URL, secret);全部未命中返回 None。"""
    for url in build_urls:
        secret = find_token_secret(fetch_text(url))
        if secret is not None:
            return url, secret
    return None


def make_token(score_id: int, jtype: str, index: int, secret: str) -> str:
    digest = hashlib.md5(f"{score_id}{jtype}{index}{secret}".encode(), usedforsecurity=False)
    return digest.hexdigest()[:4]


def jmuse_url(score_id: int, jtype: str, index: int) -> str:
    return f"https://musescore.com/api/jmuse?id={score_id}&index={index}&type={jtype}"


def parse_jmuse(http_status: int, payload: object) -> str | None:
    """返回文件 URL;url 为空或接口返回 404(文件不存在、超出页数)时返回 None。"""
    if not isinstance(payload, dict):
        raise CgdlError(PARSE_FAILED, "jmuse response is not a JSON object")
    if http_status == 404:
        return None
    if http_status != 200 or payload.get("result") != "success":
        error = str(payload.get("error") or "")[:120]
        raise CgdlError(
            DOWNLOAD_FAILED, f"jmuse refused the request (HTTP {http_status}) {error}".strip()
        )
    info: Any = payload.get("info")
    url = info.get("url") if isinstance(info, dict) else None
    if not isinstance(url, str):
        raise CgdlError(PARSE_FAILED, "jmuse response has no url field")
    return url or None
