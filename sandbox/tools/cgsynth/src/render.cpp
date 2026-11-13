#include "render.hpp"

#include <algorithm>
#include <array>
#include <cmath>
#include <cstddef>
#include <memory>
#include <set>
#include <string>
#include <utility>
#include <vector>

#include <fluidsynth.h>

namespace cgmusic {
namespace fs = std::filesystem;

RenderEngine parse_render_engine(const std::string& name) {
  if (name == "fluidsynth") return RenderEngine::FluidSynth;
  if (name == "muse") return RenderEngine::Muse;
  throw Error("bad_input", "--engine expects fluidsynth or muse");
}

const char* render_engine_name(RenderEngine engine) noexcept {
  return engine == RenderEngine::Muse ? "muse" : "fluidsynth";
}

namespace {

constexpr std::size_t kBlockFrames = 1024;
constexpr int kDrumChannel = 9;
constexpr int kDrumBank = 128;
constexpr int kBankSelectMsb = 0;
constexpr int kBankSelectLsb = 32;
constexpr const char* kFallbackReason = "preset_missing_in_font";

struct SettingsDeleter {
  void operator()(fluid_settings_t* settings) const { delete_fluid_settings(settings); }
};
struct SynthDeleter {
  void operator()(fluid_synth_t* synth) const { delete_fluid_synth(synth); }
};
using SettingsPtr = std::unique_ptr<fluid_settings_t, SettingsDeleter>;
using SynthPtr = std::unique_ptr<fluid_synth_t, SynthDeleter>;

void require(int status, const char* what) {
  if (status == FLUID_FAILED) throw Error("synth_failed", std::string("FluidSynth: ") + what);
}

SettingsPtr make_settings() {
  SettingsPtr settings(new_fluid_settings());
  if (!settings) throw Error("synth_failed", "FluidSynth settings allocation failed");
  require(fluid_settings_setnum(settings.get(), "synth.sample-rate",
                                static_cast<double>(kSampleRate)), "sample rate");
  require(fluid_settings_setint(settings.get(), "synth.polyphony", 512), "polyphony");
  require(fluid_settings_setint(settings.get(), "synth.reverb.active", 0), "disable reverb");
  require(fluid_settings_setint(settings.get(), "synth.chorus.active", 0), "disable chorus");
  return settings;
}

int load_font(fluid_synth_t* synth, const fs::path& path) {
  const int id = fluid_synth_sfload(synth, path.string().c_str(), 1);
  if (id == FLUID_FAILED) {
    throw Error("no_soundfont", "FluidSynth could not load SoundFont " + path.filename().string());
  }
  return id;
}

fluid_sfont_t* font_of(fluid_synth_t* synth, int id) {
  fluid_sfont_t* sfont = fluid_synth_get_sfont_by_id(synth, id);
  if (sfont == nullptr) throw Error("no_soundfont", "FluidSynth lost the SoundFont after loading");
  return sfont;
}

std::vector<PresetInfo> read_presets(fluid_sfont_t* sfont) {
  std::vector<PresetInfo> presets;
  fluid_sfont_iteration_start(sfont);
  while (fluid_preset_t* preset = fluid_sfont_iteration_next(sfont)) {
    const char* name = fluid_preset_get_name(preset);
    presets.push_back({fluid_preset_get_banknum(preset), fluid_preset_get_num(preset),
                       name != nullptr ? name : ""});
  }
  return presets;
}

std::set<PresetKey> preset_keys(const std::vector<PresetInfo>& presets) {
  std::set<PresetKey> keys;
  for (const PresetInfo& preset : presets) keys.insert({preset.bank, preset.program});
  return keys;
}

}  // namespace

std::vector<PresetInfo> list_font_presets(const fs::path& path) {
  const SettingsPtr settings = make_settings();
  SynthPtr synth(new_fluid_synth(settings.get()));
  if (!synth) throw Error("synth_failed", "FluidSynth synthesizer allocation failed");
  const int id = load_font(synth.get(), path);
  return read_presets(font_of(synth.get(), id));
}

Audio render_fluidsynth_channel(const MidiSequence& sequence, int channel,
                                const SoundfontInfo& soundfont, std::size_t total_frames,
                                RenderReport* report) {
  const SettingsPtr settings = make_settings();
  SynthPtr synth(new_fluid_synth(settings.get()));
  if (!synth) throw Error("synth_failed", "FluidSynth synthesizer allocation failed");
  require(fluid_synth_set_interp_method(synth.get(), -1, FLUID_INTERP_7THORDER), "interpolation");

  const int sfont_id = load_font(synth.get(), soundfont.path);
  const auto presets = preset_keys(read_presets(font_of(synth.get(), sfont_id)));
  const bool is_drum = channel == kDrumChannel ||
                       (channel >= 0 && static_cast<std::size_t>(channel) < sequence.channel_is_drum.size() &&
                        sequence.channel_is_drum[static_cast<std::size_t>(channel)]);
  const int synth_ch = is_drum ? kDrumChannel : 0;
  const PresetKey init_preset = resolve_preset(presets, PresetKey{is_drum ? kDrumBank : 0, 0});
  require(fluid_synth_program_select(synth.get(), synth_ch, sfont_id, init_preset.bank,
                                     init_preset.program), "program select");

  Audio audio;
  audio.rate = kSampleRate;
  audio.channels.assign(2, std::vector<float>(total_frames, 0.0F));
  std::array<float, kBlockFrames> left{};
  std::array<float, kBlockFrames> right{};
  std::size_t position = 0;
  const auto render_until = [&](std::size_t target) {
    target = std::min(target, total_frames);
    while (position < target) {
      const std::size_t count = std::min(kBlockFrames, target - position);
      if (fluid_synth_write_float(synth.get(), static_cast<int>(count), left.data(), 0, 1,
                                  right.data(), 0, 1) != FLUID_OK) {
        throw Error("synth_failed", "FluidSynth rendering failed");
      }
      std::copy_n(left.data(), count, audio.channels[0].begin() + static_cast<std::ptrdiff_t>(position));
      std::copy_n(right.data(), count, audio.channels[1].begin() + static_cast<std::ptrdiff_t>(position));
      position += count;
    }
  };

  std::set<PresetKey> missing;
  int bank_msb = 0;
  int bank_lsb = 0;
  for (const MidiEvent& event : sequence.events) {
    if (event.channel != channel) continue;
    render_until(static_cast<std::size_t>(std::llround(event.seconds * kSampleRate)));
    switch (event.status & 0xF0) {
      case 0x80:
        fluid_synth_noteoff(synth.get(), synth_ch, event.data1);
        break;
      case 0x90:
        if (event.data2 == 0) fluid_synth_noteoff(synth.get(), synth_ch, event.data1);
        else fluid_synth_noteon(synth.get(), synth_ch, event.data1, event.data2);
        break;
      case 0xA0:
        fluid_synth_key_pressure(synth.get(), synth_ch, event.data1, event.data2);
        break;
      case 0xB0:
        if (event.data1 == kBankSelectMsb) bank_msb = event.data2;
        else if (event.data1 == kBankSelectLsb) bank_lsb = event.data2;
        else fluid_synth_cc(synth.get(), synth_ch, event.data1, event.data2);
        break;
      case 0xC0: {
        const int bank = is_drum ? kDrumBank : (bank_msb | (bank_lsb << 7));
        const PresetKey wanted{bank, event.data1};
        const PresetKey preset = resolve_preset(presets, wanted);
        if (preset.bank != wanted.bank || preset.program != wanted.program) missing.insert(wanted);
        require(fluid_synth_program_select(synth.get(), synth_ch, sfont_id, preset.bank,
                                           preset.program), "program select");
        break;
      }
      case 0xD0:
        fluid_synth_channel_pressure(synth.get(), synth_ch, event.data1);
        break;
      case 0xE0:
        fluid_synth_pitch_bend(synth.get(), synth_ch,
                               event.data1 | (static_cast<int>(event.data2) << 7));
        break;
      default:
        break;
    }
  }
  render_until(total_frames);

  if (report) {
    FontReport font{soundfont.id, soundfont.file_name, {}};
    for (const PresetKey& wanted : missing) {
      font.missing_presets.push_back({channel + 1, wanted, kFallbackReason});
    }
    report->fonts = {std::move(font)};
    report->fallbacks = report->fonts.front().missing_presets;
  }
  return audio;
}

}  // namespace cgmusic
