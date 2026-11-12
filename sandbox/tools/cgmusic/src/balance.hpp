#pragma once

#include <array>
#include <cstddef>
#include <cstdint>

#include "midi.hpp"

namespace cgmusic {

// 电平平衡的声部:每个声部单独合成,再按偏移增益求和。
// 枚举顺序与 balance.cpp 中的 kStemLevels 一致。
enum class Stem : std::uint8_t {
  Melodic,    // 其余所有乐器
  Woodwind,   // GM 64-79:簧管与笛类
  Accordion,  // GM 21、23(22 口琴不归入)
  Brass,      // GM 56-63:铜管
  Kick,       // 鼓组音符 35、36
  Snare,      // 鼓组音符 38-40
  OtherDrum,  // 鼓组其余音符
};

inline constexpr std::size_t kStemCount = 7;

// 事件路由掩码:bit i 表示 Stem(i)。
using StemMask = std::uint8_t;
inline constexpr StemMask kAllStems = 0x7F;

constexpr StemMask stem_bit(Stem stem) noexcept {
  return static_cast<StemMask>(1U << static_cast<unsigned>(stem));
}

// 统一基础电平(dB),关闭平衡时不使用。
inline constexpr double kBalanceBaseDb = -6.0;

const char* stem_name(Stem stem) noexcept;

// 声部自身的偏移(不含基础电平)。
double stem_offset_db(Stem stem) noexcept;

// 声部增益(dB):balance 开启时为基础电平加偏移,关闭时为 0。
double stem_gain_db(Stem stem, bool balance) noexcept;

// 按通道、GM 程序号与音符号归类。第 10 通道(索引 9)为鼓组,只按音符分类。
Stem classify_note(int channel, int program, int note) noexcept;

// 逐事件路由。跟踪每通道的当前程序号与每声部内的活动音符:
// 音符开启按所属声部路由;音符关闭只发给仍持有该音符的声部;
// 其余事件(控制器、音色、弯音等)广播到所有声部。
class StemRouter {
 public:
  explicit StemRouter(bool balance) noexcept;

  StemMask route(const MidiEvent& event);

 private:
  StemMask release(int channel, int note);

  bool balance_;
  std::array<int, 16> program_{};
  std::array<std::array<std::array<bool, kStemCount>, 128>, 16> active_{};
};

}  // namespace cgmusic
