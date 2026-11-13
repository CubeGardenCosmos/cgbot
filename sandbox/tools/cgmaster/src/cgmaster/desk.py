"""按 plan.json 把干声分轨分配到干声、混响和回声总线。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pedalboard as pb
import soundfile as sf
from pedalboard import Delay, HighpassFilter, LowpassFilter, Reverb

from cgmaster.errors import BAD_INPUT, CgmasterError
from cgmaster.quality import db_to_linear, linear_to_db

SAMPLE_RATE = 44100


@dataclass
class DeskResult:
    audio: np.ndarray
    balance: list[dict[str, object]]
    channels: list[dict[str, object]]
    tracks: list[dict[str, object]]


def _finite(value: object, default: float, name: str, low: float, high: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise CgmasterError(BAD_INPUT, f"{name} 必须是数字")
    result = float(value)
    if not math.isfinite(result) or not low <= result <= high:
        raise CgmasterError(BAD_INPUT, f"{name} 须在 {low}-{high} 之间")
    return result


def _fallback_gain(part: dict[str, object]) -> float:
    """未提供最终推子电平时，复刻旧 C++ 的族级平衡规则。"""
    family = str(part.get("family", "other")).lower()
    is_drum = bool(part.get("is_drum", False))
    if is_drum or family in {"drums", "percussion", "drumline"}:
        return -6.0
    offset = {"woodwinds": -6.0, "woodwind": -6.0, "accordion": -6.0, "brass": 6.0}.get(
        family, 0.0
    )
    gain = -6.0 + offset
    name = str(part.get("muse_name", ""))
    engine = str(part.get("engine_hint", "muse"))
    if engine == "fluidsynth" or (
        engine == "muse"
        and (family in {"percussion", "drumline"} or "Drum" in name or "Perc" in name)
    ):
        gain -= 12.0
    return gain


def _load_stem(path: Path) -> np.ndarray:
    if not path.is_file():
        raise CgmasterError(BAD_INPUT, f"分轨文件不存在: {path}")
    try:
        audio, rate = sf.read(path, dtype="float32", always_2d=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise CgmasterError(BAD_INPUT, f"无法读取分轨 {path}: {exc}") from exc
    if rate != SAMPLE_RATE:
        raise CgmasterError(BAD_INPUT, f"分轨须为 44.1 kHz: {path} 实际为 {rate} Hz")
    if audio.shape[0] == 0 or audio.shape[1] not in {1, 2}:
        raise CgmasterError(BAD_INPUT, f"分轨须为单声道或双声道且非空: {path}")
    if not np.isfinite(audio).all():
        raise CgmasterError(BAD_INPUT, f"分轨含非有限采样: {path}")
    if audio.shape[1] == 1:
        audio = np.repeat(audio, 2, axis=1)
    return audio


def _send(value: object) -> float:
    result = _finite(value, 0.0, "效果发送量", 0.0, 127.0)
    return result / 127.0 if result > 1.0 else result


def mix_desk(
    parts: list[dict[str, object]], stems: Path, reverb_room: float, reverb_level: float
) -> DeskResult:
    loaded: list[tuple[dict[str, object], np.ndarray, float, float, float, float]] = []
    frames = 0
    for part in parts:
        part_id = str(part.get("id", ""))
        if not part_id:
            raise CgmasterError(BAD_INPUT, "声部缺少 id")
        audio = _load_stem(stems / f"{part_id}.wav")
        mix = part.get("mix", {})
        if not isinstance(mix, dict):
            raise CgmasterError(BAD_INPUT, f"声部 {part_id} 的 mix 必须是对象")
        gain_db = _finite(mix.get("gain_db"), _fallback_gain(part), "gain_db", -120.0, 60.0)
        pan = _finite(mix.get("pan"), 0.0, "pan", -1.0, 1.0)
        reverb_send = _send(mix.get("reverb_send"))
        echo_send = _send(mix.get("echo_send"))
        loaded.append((part, audio, gain_db, pan, reverb_send, echo_send))
        frames = max(frames, audio.shape[0])

    dry = np.zeros((frames, 2), dtype=np.float32)
    reverb_bus = np.zeros_like(dry)
    echo_bus = np.zeros_like(dry)
    balance: list[dict[str, object]] = []
    channels: list[dict[str, object]] = []
    tracks: list[dict[str, object]] = []
    for part, original, gain_db, pan, reverb_send, echo_send in loaded:
        part_id = str(part["id"])
        gain = db_to_linear(gain_db)
        angle = (pan + 1.0) * math.pi / 4.0
        audio = original * gain
        audio[:, 0] *= math.sqrt(2.0) * math.cos(angle)
        audio[:, 1] *= math.sqrt(2.0) * math.sin(angle)
        count = audio.shape[0]
        dry[:count] += audio
        reverb_bus[:count] += audio * reverb_send
        echo_bus[:count] += audio * echo_send
        family = str(part.get("family", "other"))
        balance.append(
            {
                "id": part_id,
                "family": family,
                "gain_db": gain_db,
                "pan": pan,
                "reverb_send": reverb_send,
                "echo_send": echo_send,
            }
        )
        channels.append({"id": part_id, "reverb_send": reverb_send, "echo_send": echo_send})
        rms = float(np.sqrt(np.mean(np.square(original, dtype=np.float64))))
        tracks.append({"id": part_id, "rms_dbfs": linear_to_db(rms), "gain_db": gain_db})

    board_cls: type[Any] = getattr(pb, "Pedalboard")  # noqa: B009
    wet = board_cls(
        [
            HighpassFilter(cutoff_frequency_hz=150.0),
            Reverb(
                room_size=reverb_room,
                damping=0.4,
                wet_level=1.0,
                dry_level=0.0,
                width=1.0,
            ),
            LowpassFilter(cutoff_frequency_hz=8500.0),
        ]
    )(reverb_bus.T, SAMPLE_RATE).T
    echo = Delay(delay_seconds=0.35, feedback=0.25, mix=1.0)(echo_bus.T, SAMPLE_RATE).T
    dry += wet * reverb_level + echo
    return DeskResult(dry, balance, channels, tracks)
