#pragma once

#include "common.hpp"

namespace cgmusic {

// Muse 母带压缩的固定参数。阈值在运行时按输入积分响度设置。
struct DynamicsSettings {
  double ratio = 2.5;
  double threshold_offset_db = 3.0;
  double knee_db = 6.0;
  double attack_ms = 30.0;
  double fast_release_ms = 120.0;
  double slow_release_ms = 900.0;
  double sidechain_hp_hz = 100.0;
  double lookahead_ms = 5.0;
  double limiter_margin_db = 0.5;
};

struct DynamicsReport {
  double threshold_db = 0.0;
  double ratio = 0.0;
  double max_gain_reduction_db = 0.0;
  double average_gain_reduction_db = 0.0;
  double makeup_db = 0.0;
};

// 声道联动、软拐点、侧链高通的总线压缩器;再自动补足增益至目标响度。
// 空输入或全静音沿用 master() 的 synth_failed 错误;final_quality 非空时返回最终母带测量。
DynamicsReport compress_master(Audio& audio, double target_lufs, double ceiling_dbtp,
                               const DynamicsSettings& settings = {},
                               Quality* final_quality = nullptr);

}  // namespace cgmusic
