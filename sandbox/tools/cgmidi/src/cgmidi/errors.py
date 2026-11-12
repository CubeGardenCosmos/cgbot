"""cgmidi 错误码与异常。CLI 以 JSON {"error": code, "message": ...} 输出到 stderr。"""

from __future__ import annotations

BAD_INPUT = "bad_input"
PARSE_FAILED = "parse_failed"
UNSUPPORTED_FORMAT = "unsupported_format"
OUT_OF_RANGE = "out_of_range"
WRITE_FAILED = "write_failed"


class CgmidiError(Exception):
    """带稳定错误码的可预期失败。message 为面向代理的中文说明。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
