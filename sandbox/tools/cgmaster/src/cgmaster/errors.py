"""可预期失败的稳定错误码。"""

from __future__ import annotations

BAD_INPUT = "bad_input"
SYNTH_FAILED = "synth_failed"
WRITE_FAILED = "write_failed"


class CgmasterError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
