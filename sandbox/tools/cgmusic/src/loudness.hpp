#pragma once

#include "common.hpp"

namespace cgmusic {

// EBU R128:积分响度、响度范围、真峰值(libebur128)。
Quality measure(const Audio& audio);

// 去除首尾低于 -60 dBFS 的静音;全静音时抛出 synth_failed。
void trim_silence(Audio& audio);

// 末尾线性淡出 seconds 秒,最后一帧为 0。
void fade_tail(Audio& audio, double seconds);

// 线性增益到 target_lufs;若真峰值超过 ceiling_dbtp,则迭代增益并经前瞻真峰值限制器压限,
// 使输出响度落在 target_lufs ±0.1 LU 内(无法收敛时取误差最小者),且真峰值不超过 ceiling_dbtp。
// 返回最终输出的测量结果;无法满足真峰值上限时抛出 synth_failed。
Quality master(Audio& audio, double target_lufs, double ceiling_dbtp);

}  // namespace cgmusic
