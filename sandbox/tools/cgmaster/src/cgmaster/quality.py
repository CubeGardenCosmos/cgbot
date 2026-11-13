"""EBU R128 响度、LRA 与 4 倍过采样真峰值。"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pyloudnorm as pyln
from scipy import signal

from cgmaster.errors import SYNTH_FAILED, CgmasterError


@dataclass(frozen=True)
class Quality:
    lufs: float
    true_peak_dbtp: float
    lra: float
    clipped: bool
    duration_s: float


def db_to_linear(db: float) -> float:
    return 10.0 ** (db / 20.0)


def linear_to_db(value: float) -> float:
    return 20.0 * math.log10(value) if value > 0 else -math.inf


def true_peak(audio: np.ndarray, rate: int) -> float:
    """声道形状的 (samples, channels) 音频，用 4x 多相滤波测真峰值。"""
    oversampled = signal.resample_poly(audio, 4, 1, axis=0)
    return float(np.max(np.abs(oversampled), initial=0.0))


def measure(audio: np.ndarray, rate: int, with_lra: bool = True) -> Quality:
    if audio.ndim != 2 or audio.shape[0] == 0 or audio.shape[1] == 0:
        raise CgmasterError(SYNTH_FAILED, "乐谱渲染结果为静音")
    try:
        meter = pyln.Meter(rate, block_size=0.400)
        lufs = float(meter.integrated_loudness(audio))
        lra = float(meter.loudness_range(audio)) if with_lra else 0.0
    except (ValueError, ZeroDivisionError, FloatingPointError) as exc:
        raise CgmasterError(SYNTH_FAILED, f"响度测量失败: {exc}") from exc
    if not math.isfinite(lufs):
        raise CgmasterError(SYNTH_FAILED, "乐谱渲染结果为静音")
    return Quality(
        lufs=lufs,
        true_peak_dbtp=linear_to_db(true_peak(audio, rate)),
        lra=lra if math.isfinite(lra) else 0.0,
        clipped=bool(np.max(np.abs(audio), initial=0.0) >= 1.0),
        duration_s=audio.shape[0] / rate,
    )
