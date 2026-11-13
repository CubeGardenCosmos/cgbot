"""稳定错误码与 JSON 错误输出(stderr 单行 JSON,非零退出)。"""

from __future__ import annotations

import json

BAD_INPUT = "bad_input"
UNSUPPORTED = "unsupported"
TOO_LONG = "too_long"
SYNTH_FAILED = "synth_failed"
WRITE_FAILED = "write_failed"
MUSE_UNAVAILABLE = "muse_unavailable"


class CgmusicError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def to_json(self) -> str:
        return json.dumps({"error": self.code, "message": self.message}, ensure_ascii=False)


def from_cgsynth_stderr(text: str, returncode: int) -> CgmusicError:
    """把 cgsynth 的 JSON 错误原样映射为 cgmusic 错误;无法解析时归为 synth_failed。"""
    for line in reversed(text.strip().splitlines()):
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("error"), str):
            return CgmusicError(obj["error"], str(obj.get("message", "")))
    detail = text.strip() or f"exit code {returncode}"
    return CgmusicError(SYNTH_FAILED, f"cgsynth 失败: {detail}")
