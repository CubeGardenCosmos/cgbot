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

enum class RenderEngine { FluidSynth, Muse };

// 引擎名只接受 fluidsynth / muse;muse 不可用时由调用方报错,不在此回退。
RenderEngine parse_render_engine(const std::string& name);
const char* render_engine_name(RenderEngine engine) noexcept;

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

// 渲染单个 MIDI 通道的干声(44.1 kHz 立体声 float),使用指定的 SoundFont。
// 混响与合唱在合成器内部关闭,由外部混音器统一处理。
Audio render_fluidsynth_channel(const MidiSequence& sequence, int channel,
                                const SoundfontInfo& soundfont, const RenderOptions& options,
                                std::size_t total_frames);

struct MuseTrackReport {
  int channel;  // 1-16
  int program;
  int instrument_id;
  std::string instrument;
  std::string family;
  std::size_t note_count = 0;
};

struct MuseRenderResult {
  std::vector<MuseTrackReport> tracks;
  std::vector<MissingPreset> fallbacks;  // 回退到 FluidSynth 的通道记录
  std::string version;
  double reverb_room = 0.0;
  double reverb_level = 0.0;
  double dry_seconds = 0.0;
  std::string mix_report;  // 混音器的 JSON 报告;已含最终响度
};

// 44.1 kHz 立体声,经 Muse 采样器按通道输出干声,再经 src/mix 混音与母带。
// 任一通道没有可用的 Muse 乐器、库失败或输出静音均报错;不回退 FluidSynth。
Audio render_sequence_muse(const MidiSequence& sequence, const RenderOptions& options,
                           double target_lufs, MuseRenderResult* report);

}  // namespace cgmusic
