"""cgdl 错误码与异常。CLI 以 JSON {"error": code, "message": ...} 输出到 stderr。"""

from __future__ import annotations

BLOCKED = "blocked"
NOT_FOUND = "not_found"
PARSE_FAILED = "parse_failed"
DOWNLOAD_FAILED = "download_failed"
UNSUPPORTED_KIND = "unsupported_kind"
BAD_URL = "bad_url"
# 以下两项为规范之外的补充错误码:命令行参数错误、无法启动 Chromium。
BAD_INPUT = "bad_input"
BROWSER_UNAVAILABLE = "browser_unavailable"


class CgdlError(Exception):
    """带稳定错误码的可预期失败。message 不得包含令牌、签名 URL 或 Cookie。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
