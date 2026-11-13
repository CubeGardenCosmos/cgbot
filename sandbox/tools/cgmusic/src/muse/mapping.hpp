#pragma once

#include <optional>
#include <string_view>

#include "muse/catalog.hpp"

namespace cgmusic {

// GM 程序号使用 0 基(0-127);is_drum 对应 MIDI 通道 10。
// 没有已安装的候选音色时返回 nullopt,由调用方回退到 FluidSynth。
// GM 电钢琴(4、5)固定返回 nullopt,保留默认 DX7 SoundFont。
std::optional<int> pick_instrument(int gm_program, bool is_drum, const Catalog& catalog);

// 按声部名称(来自 MusicXML 或 SMF 轨道名)优先查找乐器;
// 名称为空或无匹配时回退到 pick_instrument(gm_program, is_drum, catalog)。
// part_name 为原始声部名(区分大小写的部分名词匹配);is_drum 覆盖名称判断。
std::optional<int> pick_instrument_by_name(std::string_view part_name, int gm_program,
                                           bool is_drum, const Catalog& catalog,
                                           int occurrence = 0);

}  // namespace cgmusic
