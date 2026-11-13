#pragma once

#include <optional>

#include "muse/catalog.hpp"

namespace cgmusic {

// GM 程序号使用 0 基(0-127);is_drum 对应 MIDI 通道 10。
// 没有已安装的候选音色时返回 nullopt,由调用方回退到 FluidSynth。
// GM 电钢琴(4、5)固定返回 nullopt,保留默认 DX7 SoundFont。
std::optional<int> pick_instrument(int gm_program, bool is_drum, const Catalog& catalog);

}  // namespace cgmusic
