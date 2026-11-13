#pragma once

#include <cstddef>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

#include "balance.hpp"

namespace cgmusic {

// 一个干声部(44.1 kHz 浮点,左右声道等长)。
struct MixInput {
  std::string family;  // Muse 声部族名,如 Strings、Brass;未知族名归入 Melodic。
  int channel = 0;     // 来源 MIDI 通道(1-16),仅用于报告。
  std::vector<float> left;
  std::vector<float> right;
  std::optional<int> reverb_send;  // 0-127;nullopt 使用族默认值;0 强制干声。
};

struct MixOptions {
  double reverb_room = 0.75;   // 0-1,VideoMaker 预设 room_size。
  double reverb_level = 1.0;   // 0-1.5,湿声总电平(与 VideoMaker 的湿度上限 1.5 一致)。
  std::optional<double> target_lufs = -14.0;  // nullopt:保持混音电平,仅施加 alimiter 式上限(-0.4455 dBTP)。
  bool auto_balance = true;    // 自动声部平衡(balance.hpp);关闭仅用于开发。
  bool master_dynamics = false;  // 仅 Muse 路径开启;false 保持原母带逐采样不变。
};

struct MixResult {
  std::vector<float> left;
  std::vector<float> right;
  std::string report;  // JSON 对象文本(调用方直接嵌入输出)。
};

// 族默认混响发送(0-127)。Muse 元数据不含混响量,这些值为 cgmusic 自行调校。
int default_reverb_send(std::string_view family) noexcept;

// 族名映射到 balance.hpp 的声部;Kick/Snare 需要音符信息,族级混音不区分。
Stem stem_for_family(std::string_view family) noexcept;

// 自动平衡 -> 每声部发送到共享总线 -> 总线混响 -> 与干声相加 -> 响度/真峰值母带(loudness.hpp)。
// 输入不被修改。输入非法或长度不一致时抛出 bad_input。
MixResult mix_stems(const std::vector<MixInput>& inputs, const MixOptions& options);

}  // namespace cgmusic
