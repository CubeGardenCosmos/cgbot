#pragma once

#include <array>
#include <cstddef>
#include <filesystem>
#include <optional>
#include <string>
#include <vector>

#include "common.hpp"
#include "midi.hpp"
#include "program_map.hpp"
#include "soundfont.hpp"

namespace cgmusic {

// 混响的显式覆盖(由调用方指定);未设置的项使用默认发送表与 FluidSynth 默认值。
struct ReverbOptions {
  // 索引 0-15 对应 MIDI 通道 1-16;取值 0-127(CC91)。
  std::array<std::optional<int>, 16> channel_send{};
  std::optional<double> room;   // synth.reverb.room-size,0-1
  std::optional<double> level;  // synth.reverb.level,0-1
};

// --font-for-channel:把一个 MIDI 通道绑定到指定字体(其余通道使用主字体)。
struct FontOverride {
  int channel;  // 索引 0-15,对应 MIDI 通道 1-16
  SoundfontInfo font;
};

struct RenderOptions {
  bool balance = true;  // 声部电平平衡;由内部开关控制,不对外暴露
  ReverbOptions reverb;
  std::vector<FontOverride> font_overrides;
};

struct AppliedStem {
  std::string name;
  double offset_db;  // 声部偏移(关闭平衡时为 0)
  double gain_db;    // 实际应用的增益
};

struct ChannelReport {
  int channel;                // 1-16
  int program;                // 最后一次程序号
  int reverb;                 // 最终 CC91
  std::string reverb_source;  // 来源:table / file / cli
  int chorus;                 // 最终 CC93
  std::string chorus_source;  // 来源:table / file / cli
};

// 通道请求的音色在其所属的额外字体中不存在,已改用主字体中最接近的音色。
struct MissingPreset {
  int channel;  // 1-16
  PresetKey wanted;
  std::string reason;  // 固定为 preset_missing_in_font
};

// 单个字体的报告;下标 0 为主字体。
struct FontReport {
  std::string id;
  std::string file;
  std::vector<int> channels;               // 实际发出音符、绑定到该字体的通道 1-16
  std::vector<MissingPreset> missing_presets;  // 绑定到该字体、但字体中缺失的音色
};

struct RenderReport {
  std::vector<AppliedStem> stems;
  std::vector<ChannelReport> channels;  // 仅列出出现过音符的通道
  double reverb_room = 0.0;
  double reverb_level = 0.0;
  std::vector<FontReport> fonts;        // 第一项为主字体
  std::vector<MissingPreset> fallbacks;  // 全部回退到主字体的请求,与 fonts[].missing_presets 一致
};

// SoundFont 中的一个音色及其名称。
struct PresetInfo {
  int bank;
  int program;
  std::string name;
};

// 载入字体并按 FluidSynth 的迭代顺序列出全部音色。
std::vector<PresetInfo> list_font_presets(const std::filesystem::path& path);

// 输出 44.1 kHz 立体声 float,末尾包含混响尾音;尚未做响度处理。
// 按声部分别合成后按电平偏移求和;report 非空时记录实际使用的声部、发送量与字体信息。
Audio render_sequence(const MidiSequence& sequence, const SoundfontInfo& primary,
                      const RenderOptions& options, RenderReport* report);

}  // namespace cgmusic
