#pragma once

#include <cstddef>
#include <filesystem>
#include <string>
#include <vector>

#include "common.hpp"
#include "midi.hpp"
#include "program_map.hpp"
#include "soundfont.hpp"

namespace cgmusic {

enum class RenderEngine { FluidSynth, Muse };
RenderEngine parse_render_engine(const std::string& name);
const char* render_engine_name(RenderEngine engine) noexcept;

// 回退或缺失的音色。channel 为 1-16。
struct MissingPreset {
  int channel;
  PresetKey wanted;
  std::string reason;
};

struct FontReport {
  std::string id;
  std::string file;
  std::vector<MissingPreset> missing_presets;
};

struct RenderReport {
  std::vector<FontReport> fonts;
  std::vector<MissingPreset> fallbacks;
};

struct PresetInfo {
  int bank;
  int program;
  std::string name;
};

std::vector<PresetInfo> list_font_presets(const std::filesystem::path& path);

// 单个声部的干声：44.1 kHz 立体声 float，禁用 FluidSynth 内部混响与合唱。
// total_frames 包含 MIDI 结束后的释音尾巴。
Audio render_fluidsynth_channel(const MidiSequence& sequence, int channel,
                                const SoundfontInfo& soundfont, std::size_t total_frames,
                                RenderReport* report = nullptr);

struct MuseTrackReport {
  int channel;
  int program;
  int instrument_id;
  std::string instrument;
  std::string family;
  std::size_t note_count = 0;
};

struct MuseRenderResult {
  std::vector<MuseTrackReport> tracks;
  std::vector<MissingPreset> fallbacks;
  std::string version;
  double dry_seconds = 0.0;
};

// 只渲染单个 Muse 声部，不做混音、增益、响度或编码处理。
Audio render_sequence_muse(const MidiSequence& sequence, MuseRenderResult* report);

}  // namespace cgmusic
