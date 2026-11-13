"""Muse 路径的总线压缩、前瞻限制与响度归一化。"""

from __future__ import annotations

import math

import numpy as np
from pedalboard import BrickwallLimiter, Compressor

from cgmaster.errors import SYNTH_FAILED, CgmasterError
from cgmaster.quality import Quality, db_to_linear, linear_to_db, measure, true_peak

TARGET_LUFS = -14.0
CEILING_DBTP = -1.5
LOUDNESS_TOLERANCE = 0.1


def trim_and_fade(audio: np.ndarray, rate: int, fade_seconds: float = 0.5) -> np.ndarray:
    audible = np.flatnonzero(np.max(np.abs(audio), axis=1) > 0.001)
    if audible.size == 0:
        raise CgmasterError(SYNTH_FAILED, "乐谱渲染结果为静音")
    result = audio[int(audible[0]) : int(audible[-1]) + 1].copy()
    length = min(result.shape[0], int(fade_seconds * rate))
    if length:
        result[-length:] *= np.arange(length - 1, -1, -1, dtype=np.float32)[:, None] / length
    return result


def _limit(audio: np.ndarray, rate: int, ceiling: float) -> np.ndarray:
    # BrickwallLimiter 比 pedalboard.Limiter 多一个可调 ceiling，且无自动补偿增益。
    target = ceiling
    threshold = ceiling
    # 4x 侧链仍可能有少量过采样过冲；按实测 TP 迭代收紧，不累积重复压限。
    for _ in range(8):
        result = BrickwallLimiter(
            ceiling_db=threshold, release_ms=100.0, lookahead_ms=5.0, true_peak=True
        )(audio, rate)
        overshoot = linear_to_db(true_peak(result, rate)) - target
        if overshoot <= 0:
            return result
        threshold -= overshoot + 0.01
    raise CgmasterError(SYNTH_FAILED, "无法将真峰值限制到指定上限")


def normalize(audio: np.ndarray, rate: int, target: float) -> np.ndarray:
    measured = measure(audio, rate, with_lra=False)
    linear_db = target - measured.lufs
    if measured.true_peak_dbtp + linear_db <= CEILING_DBTP:
        return audio * db_to_linear(linear_db)

    def run(offset_db: float) -> tuple[np.ndarray, Quality]:
        limited = _limit(audio * db_to_linear(offset_db), rate, CEILING_DBTP)
        return limited, measure(limited, rate, with_lra=False)

    offset_db = linear_db
    best_offset = offset_db
    best_error = math.inf
    best_audio: np.ndarray | None = None
    previous: tuple[float, float] | None = None
    for _ in range(6):
        current, quality = run(offset_db)
        error = quality.lufs - target
        if abs(error) < best_error:
            best_error = abs(error)
            best_offset = offset_db
            best_audio = current
        if abs(error) <= LOUDNESS_TOLERANCE:
            return current
        slope = 1.0
        if previous is not None and abs(offset_db - previous[0]) > 1e-6:
            slope = (quality.lufs - previous[1]) / (offset_db - previous[0])
        slope = min(1.5, max(0.1, slope))
        previous = (offset_db, quality.lufs)
        offset_db += min(6.0, max(-6.0, -error / slope))
    if best_audio is None:
        best_audio, _ = run(best_offset)
    return best_audio


def master_audio(audio: np.ndarray, rate: int, target: float) -> np.ndarray:
    # 与 C++ mix_hybrid 一致：Muse 合成路径才启用总线压缩。
    input_quality = measure(audio, rate, with_lra=False)
    compressed = Compressor(
        threshold_db=input_quality.lufs + 3.0,
        ratio=2.5,
        attack_ms=30.0,
        release_ms=120.0,
    )(audio, rate)
    compressed_quality = measure(compressed, rate, with_lra=False)
    compressed *= db_to_linear(target - compressed_quality.lufs)
    pre_limited = _limit(compressed, rate, CEILING_DBTP - 0.5)
    normalized = normalize(pre_limited, rate, target)
    if true_peak(normalized, rate) > db_to_linear(CEILING_DBTP) + 1e-7:
        raise CgmasterError(SYNTH_FAILED, "无法将真峰值限制到 -1.5 dBTP 以下")
    return normalized
