"""cgmusic analyze:用 cgmaster 的 EBU R128 响度与 4 倍过采样真峰值测量已有音频。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import soundfile as sf
from cgmaster.errors import CgmasterError
from cgmaster.quality import measure

from cgmusic.errors import BAD_INPUT, CgmusicError


def analyze(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise CgmusicError(BAD_INPUT, f"音频文件不存在: {path}")
    try:
        audio, rate = sf.read(str(path), dtype="float32", always_2d=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise CgmusicError(BAD_INPUT, f"无法读取音频: {exc}") from exc
    try:
        quality = measure(audio, rate)
    except CgmasterError as exc:
        raise CgmusicError(exc.code, exc.message) from exc
    return {
        "lufs": quality.lufs,
        "true_peak_dbtp": quality.true_peak_dbtp,
        "lra": quality.lra,
        "clipped": quality.clipped,
        "duration_s": quality.duration_s,
    }
