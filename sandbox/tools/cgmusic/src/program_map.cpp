#include "program_map.hpp"

#include <cstdlib>
#include <optional>

#include "common.hpp"

namespace cgmusic {
namespace {

// 在 bank 中找程序号最接近 program 的音色;同距离时保留较小者(set 按升序遍历)。
std::optional<PresetKey> nearest_in_bank(const std::set<PresetKey>& available, int bank,
                                         int program) {
  std::optional<PresetKey> best;
  int best_distance = 0;
  for (const PresetKey& preset : available) {
    if (preset.bank != bank) continue;
    const int distance = std::abs(preset.program - program);
    if (!best || distance < best_distance) {
      best = preset;
      best_distance = distance;
    }
  }
  return best;
}

}  // namespace

PresetKey resolve_preset(const std::set<PresetKey>& available, PresetKey wanted) {
  if (available.empty()) throw Error("no_soundfont", "SoundFont contains no presets");
  if (available.count(wanted) != 0) return wanted;
  if (const auto same_bank = nearest_in_bank(available, wanted.bank, wanted.program)) {
    return *same_bank;
  }
  if (wanted.bank != 0) {
    if (const auto melodic = nearest_in_bank(available, 0, wanted.program)) return *melodic;
  }
  return *available.begin();
}

}  // namespace cgmusic
