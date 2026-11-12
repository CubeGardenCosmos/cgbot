"""/api/jmuse 的 token 推导与响应解析。

token 与页面脚本一致:md5(id + type + index + secret) 的前 4 位十六进制。
secret 由页面构建脚本中的 `+"<secret>").substr(0,4)` 模式取得,不写死在代码里。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from cgdl.errors import DOWNLOAD_FAILED, PARSE_FAILED, CgdlError

_SECRET_RE = re.compile(r'\w\+\w\+\w\+"([^"\\]{1,64})"\)\.substr\(0,\s*4\)')


def find_token_secret(script_text: str) -> str | None:
    match = _SECRET_RE.search(script_text)
    return match[1] if match else None


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
