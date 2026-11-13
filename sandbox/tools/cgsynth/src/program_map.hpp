#pragma once

#include <set>

namespace cgmusic {

// SoundFont 中的一个音色:bank(0 为旋律库,128 为鼓组)与 GM 程序号。
struct PresetKey {
  int bank = 0;
  int program = 0;

  friend bool operator==(const PresetKey& a, const PresetKey& b) {
    return a.bank == b.bank && a.program == b.program;
  }
  friend bool operator<(const PresetKey& a, const PresetKey& b) {
    return a.bank != b.bank ? a.bank < b.bank : a.program < b.program;
  }
};

// 为 wanted 选取字体中实际存在的音色,顺序为:
// 精确匹配 -> 同 bank 中程序号最接近者(同距离取较小者) -> bank 0 中最接近者 -> 最小的可用音色。
// available 为空时抛出 no_soundfont。
PresetKey resolve_preset(const std::set<PresetKey>& available, PresetKey wanted);

}  // namespace cgmusic
