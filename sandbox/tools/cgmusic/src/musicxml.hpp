#pragma once

// MusicXML 转 MIDI：支持 .musicxml/.xml（partwise 与 timewise）以及 .mxl 压缩包。
// 输出 SMF 格式 1，480 PPQ。失败时抛出 cgmusic::Error，错误码为 "bad_musicxml"。

#include <cstdint>
#include <string_view>
#include <vector>

namespace cgmusic {

std::vector<std::uint8_t> musicxml_to_midi(const std::vector<std::uint8_t>& data,
                                           std::string_view filename = {});

}  // namespace cgmusic
