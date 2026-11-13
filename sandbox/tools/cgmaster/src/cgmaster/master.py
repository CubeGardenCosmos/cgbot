"""plan.json + 干声分轨到母带文件的完整管线。"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf
from pedalboard.io import AudioFile

from cgmaster.desk import mix_desk
from cgmaster.dynamics import CEILING_DBTP, TARGET_LUFS, master_audio, trim_and_fade
from cgmaster.errors import BAD_INPUT, WRITE_FAILED, CgmasterError
from cgmaster.quality import Quality, measure


def _load_plan(plan_path: Path) -> dict[str, Any]:
    if not plan_path.is_file():
        raise CgmasterError(BAD_INPUT, f"plan.json 不存在: {plan_path}")
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CgmasterError(BAD_INPUT, f"无法读取 plan.json: {exc}") from exc
    if not isinstance(plan, dict) or not isinstance(plan.get("parts"), list) or not plan["parts"]:
        raise CgmasterError(BAD_INPUT, "plan.json 必须包含非空 parts 数组")
    if not all(isinstance(part, dict) for part in plan["parts"]):
        raise CgmasterError(BAD_INPUT, "plan.json parts 必须都是对象")
    return plan


def _number(value: float) -> float | None:
    return value if math.isfinite(value) else None


def _quality_json(quality: Quality) -> dict[str, object]:
    return {
        "duration_s": quality.duration_s,
        "lufs": _number(quality.lufs),
        "true_peak_dbtp": _number(quality.true_peak_dbtp),
        "lra": _number(quality.lra),
        "clipped": quality.clipped,
    }


def _encode(audio: np.ndarray, rate: int, output: Path, fmt: str) -> None:
    if not output.parent.is_dir():
        raise CgmasterError(BAD_INPUT, f"输出目录不存在: {output.parent}")
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp{output.suffix}")
    try:
        if fmt == "mp3":
            with AudioFile(
                str(temporary), "w", samplerate=rate, num_channels=2, quality=192
            ) as stream:
                stream.write(audio.T)
        else:
            sf.write(
                temporary,
                audio,
                rate,
                format=fmt.upper(),
                subtype="FLOAT" if fmt == "wav" else "PCM_24",
            )
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, output)
    except (OSError, RuntimeError, ValueError) as exc:
        raise CgmasterError(WRITE_FAILED, f"音频写入失败: {exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)


def master(
    plan: str | Path,
    stems: str | Path,
    out: str | Path,
    *,
    fmt: str = "mp3",
    gain: float = 0.0,
    reverb_room: float = 0.75,
    reverb_level: float = 1.0,
) -> dict[str, object]:
    """混音并写出母带，返回可直接 JSON 序列化的报告。"""
    fmt = fmt.lower()
    if fmt not in {"mp3", "wav", "flac"}:
        raise CgmasterError(BAD_INPUT, f"不支持的输出格式: {fmt}")
    if not math.isfinite(gain):
        raise CgmasterError(BAD_INPUT, "gain 必须为有限数值")
    gain = min(12.0, max(-12.0, gain))
    if not 0.0 <= reverb_room <= 1.0:
        raise CgmasterError(BAD_INPUT, "reverb room 须在 0-1 之间")
    if not 0.0 <= reverb_level <= 1.5:
        raise CgmasterError(BAD_INPUT, "reverb level 须在 0-1.5 之间")

    plan_data = _load_plan(Path(plan))
    stems_path = Path(stems)
    if not stems_path.is_dir():
        raise CgmasterError(BAD_INPUT, f"分轨目录不存在: {stems_path}")
    desk = mix_desk(plan_data["parts"], stems_path, reverb_room, reverb_level)
    processed = trim_and_fade(desk.audio, 44100)
    processed = master_audio(processed, 44100, TARGET_LUFS + gain)
    quality = measure(processed, 44100)
    if quality.true_peak_dbtp > CEILING_DBTP + 1e-5:
        raise CgmasterError(BAD_INPUT, "母带真峰值超过 -1.5 dBTP")

    output = Path(out)
    _encode(processed, 44100, output, fmt)
    return {
        "path": str(output.absolute()),
        "fmt": fmt,
        **_quality_json(quality),
        "balance": desk.balance,
        "reverb": {"room": reverb_room, "level": reverb_level, "channels": desk.channels},
        "tracks": desk.tracks,
    }
