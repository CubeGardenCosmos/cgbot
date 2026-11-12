"""输出目录与文件名。文件以原子方式写入:先写临时文件,fsync 后 rename。"""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
from pathlib import Path

from cgdl.errors import BAD_INPUT, CgdlError

DEFAULT_OUTPUT_DIR = "/workspace/artifacts/music"
_UNSAFE = re.compile(r"[\x00-\x1f\x7f\s/\\:*?\"<>|]+")
_MAX_TITLE_CHARS = 64


def sanitize_stem(title: str, score_id: int) -> str:
    """由标题生成安全文件名主干,形如 `Title_Words-6969530`。不含路径分隔符,不以点开头。"""
    cleaned = _UNSAFE.sub("_", title).strip("._")[:_MAX_TITLE_CHARS].strip("._")
    return f"{cleaned or 'score'}-{score_id}"


def ensure_output_dir(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise CgdlError(BAD_INPUT, "output directory must be an absolute path")
    path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise CgdlError(BAD_INPUT, "output path exists and is not a directory")
    return path


def atomic_write(dest: Path, data: bytes) -> None:
    """原子写入:同目录临时文件 -> fsync -> chmod 0644 -> os.replace。失败时删除临时文件。"""
    if dest.name in ("", ".", "..") or "/" in dest.name:
        raise CgdlError(BAD_INPUT, "refusing to write outside the output directory")
    fd, tmp_name = tempfile.mkstemp(prefix=f".{dest.name}.", suffix=".part", dir=dest.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, dest)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise
