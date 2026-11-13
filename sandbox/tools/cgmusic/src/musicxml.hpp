#pragma once

// MusicXML 转 MIDI：支持 .musicxml/.xml（partwise 与 timewise）以及 .mxl 压缩包。
// 输出 SMF 格式 1，480 PPQ。失败时抛出 cgmusic::Error，错误码为 \"bad_musicxml\"。

#include <cstdint>
#include <string_view>
#include <vector>

#include "midi.hpp"

namespace cgmusic {

// 仅输出 SMF 字节流（原始 MIDI 文件），不含 MusicXML 扩展元数据。
// FluidSynth 路径与 MIDI 文件导出使用此接口;输出字节与 musicxml_to_sequence 完全一致。
std::vector<std::uint8_t> musicxml_to_midi(const std::vector<std::uint8_t>& data,
                                           std::string_view filename = {});

// 直接返回带 channel_names 与 pizzicato_ranges 扩展字段的 MidiSequence,避免 SMF 往返丢失信息。
// Muse 渲染引擎使用此接口;MIDI 文件输入路径无此信息(见文档 §22)。
MidiSequence musicxml_to_sequence(const std::vector<std::uint8_t>& data,
                                  std::string_view filename = {});

}  // namespace cgmusic
