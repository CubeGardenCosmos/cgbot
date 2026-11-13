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

#include "balance.hpp"
#include "program_map.hpp"
#include "sends.hpp"

namespace cgmusic {

RenderEngine parse_render_engine(const std::string& name) {
  if (name == "fluidsynth") return RenderEngine::FluidSynth;
  if (name == "muse") return RenderEngine::Muse;
  throw Error("bad_input", "--engine expects fluidsynth or muse");
}

const char* render_engine_name(RenderEngine engine) noexcept {
  return engine == RenderEngine::Muse ? "muse" : "fluidsynth";
}

namespace {

namespace fs = std::filesystem;

constexpr double kTailSeconds = 4.0;  // 混响与释音尾巴
constexpr std::size_t kBlockFrames = 1024;
constexpr int kDrumChannel = 9;  // MIDI 通道 10
constexpr int kDrumBank = 128;
constexpr int kBankSelectMsb = 0;  // CC0
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

SettingsPtr make_settings(const ReverbOptions& reverb) {
  SettingsPtr settings(new_fluid_settings());
  if (!settings) throw Error("synth_failed", "FluidSynth settings allocation failed");
  require(fluid_settings_setnum(settings.get(), "synth.sample-rate",
                                static_cast<double>(kSampleRate)),
          "sample rate");
  require(fluid_settings_setint(settings.get(), "synth.polyphony", 512), "polyphony");
  require(fluid_settings_setint(settings.get(), "synth.reverb.active", 1), "reverb");
  require(fluid_settings_setint(settings.get(), "synth.chorus.active", 1), "chorus");
  if (reverb.room) {
    require(fluid_settings_setnum(settings.get(), "synth.reverb.room-size", *reverb.room),
            "reverb room");
  }
  if (reverb.level) {
    require(fluid_settings_setnum(settings.get(), "synth.reverb.level", *reverb.level),
            "reverb level");
  }
  return settings;
}

double read_setting(const SettingsPtr& settings, const char* name) {
  double value = 0.0;
  require(fluid_settings_getnum(settings.get(), name, &value), name);
  return value;
}

// 载入字体;之后所有通道都会由 select_program 显式设置音色。
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

// 本次渲染的字体表。下标 0 为主字体;channel_font 给出每个通道(索引 0-15)所属字体的下标。
struct FontSet {
  std::vector<SoundfontInfo> fonts;
  std::array<int, 16> channel_font{};
};

// 渲染过程中收集的缺失音色。各声部都会执行同样的程序号设置,因此结果以集合去重。
// missing[f] 中的元素为 (通道索引 0-15, 请求的音色),仅记录额外字体(f != 0)。
struct SelectTrace {
  std::vector<std::set<std::pair<int, PresetKey>>> missing;
};

// 按通道绑定音色所需的状态。bank 来自 CC0(不使用 CC32,常用 SoundFont 的 bank 均在 0-128 之间)。
struct ProgramBinding {
  std::vector<int> sfont_ids;                // 按字体下标
  std::vector<std::set<PresetKey>> presets;  // 按字体下标
  std::array<int, 16> channel_font{};
  std::array<int, 16> bank_msb{};
  SelectTrace* trace = nullptr;
};

// 选择通道音色。额外字体中缺失的音色改用主字体中最接近者,并记录到 trace。
void select_program(fluid_synth_t* synth, ProgramBinding& binding, int channel, int program) {
  // 鼓组通道固定使用 bank 128,与 MIDI 惯例一致。
  const int bank = channel == kDrumChannel ? kDrumBank : binding.bank_msb[channel];
  const PresetKey wanted{bank, program};
  int font = binding.channel_font[channel];
  if (font != 0 && binding.presets[font].count(wanted) == 0) {
    binding.trace->missing[font].insert({channel, wanted});
    font = 0;
  }
  const PresetKey preset = resolve_preset(binding.presets[font], wanted);
  require(fluid_synth_program_select(synth, channel, binding.sfont_ids[font], preset.bank,
                                     preset.program),
          "program select");
}

// 把通道的当前发送量发给合成器(CC91 与 CC93 成对发送,重复发送无副作用)。
void apply_sends(fluid_synth_t* synth, int channel, const ChannelSends& state) {
  require(fluid_synth_cc(synth, channel, kReverbCc, state.reverb), "reverb send");
  require(fluid_synth_cc(synth, channel, kChorusCc, state.chorus), "chorus send");
}

void dispatch(fluid_synth_t* synth, ProgramBinding& binding, const CliReverb& cli_reverb,
              std::array<ChannelSends, 16>& sends, const MidiEvent& event) {
  const int channel = event.status & 0x0F;
  switch (event.status & 0xF0) {
    case 0x80:
      fluid_synth_noteoff(synth, channel, event.data1);
      break;
    case 0x90:
      if (event.data2 == 0) {
        fluid_synth_noteoff(synth, channel, event.data1);
      } else {
        fluid_synth_noteon(synth, channel, event.data1, event.data2);
      }
      break;
    case 0xA0:
      fluid_synth_key_pressure(synth, channel, event.data1, event.data2);
      break;
    case 0xB0:
      if (event.data1 == kBankSelectMsb) {
        // 银行选择只记录,下一次程序号变化时按字体实际音色解析;不转发给 FluidSynth,以免与 program_select 冲突。
        binding.bank_msb[channel] = event.data2;
        break;
      }
      if (event.data1 == kReverbCc || event.data1 == kChorusCc) {
        update_sends(cli_reverb, channel, event, sends[channel]);
        apply_sends(synth, channel, sends[channel]);
      } else {
        fluid_synth_cc(synth, channel, event.data1, event.data2);
      }
      break;
    case 0xC0:
      select_program(synth, binding, channel, event.data1);
      update_sends(cli_reverb, channel, event, sends[channel]);
      apply_sends(synth, channel, sends[channel]);
      break;
    case 0xD0:
      fluid_synth_channel_pressure(synth, channel, event.data1);
      break;
    case 0xE0:
      fluid_synth_pitch_bend(synth, channel,
                             event.data1 | (static_cast<int>(event.data2) << 7));
      break;
    default:
      break;
  }
}

// 一次性的事件规划:路由掩码、参与渲染的声部、各通道的最终发送状态。
struct Plan {
  std::vector<StemMask> masks;
  StemMask used = 0;
  std::array<ChannelSends, 16> channels{};
};

Plan plan_events(const MidiSequence& sequence, const RenderOptions& options) {
  Plan plan;
  for (int channel = 0; channel < 16; ++channel) {
    plan.channels[channel] = initial_sends(options.reverb.channel_send, channel);
  }
  StemRouter router(options.balance);
  plan.masks.reserve(sequence.events.size());
  for (const MidiEvent& event : sequence.events) {
    const StemMask mask = router.route(event);
    plan.masks.push_back(mask);
    const int channel = event.status & 0x0F;
    ChannelSends& state = plan.channels[channel];
    update_sends(options.reverb.channel_send, channel, event, state);
    if ((event.status & 0xF0) == 0x90 && event.data2 != 0) {
      plan.used = static_cast<StemMask>(plan.used | mask);
      state.used = true;
    }
  }
  // 没有音符时仍渲染旋律声部,以便字体错误照常报告;之后由 trim_silence 报告静音。
  if (plan.used == 0) plan.used = stem_bit(Stem::Melodic);
  return plan;
}

// 渲染一个声部:只派发掩码包含 bit 的音符事件,其余事件(控制器、音色等)按路由广播到每个声部。
// 结果按增益累加到 sum。
void render_stem(const MidiSequence& sequence, const Plan& plan, StemMask bit, float gain,
                 const FontSet& fonts, const RenderOptions& options, Audio& sum,
                 SelectTrace& trace) {
  const SettingsPtr settings = make_settings(options.reverb);
  SynthPtr synth(new_fluid_synth(settings.get()));
  if (!synth) throw Error("synth_failed", "FluidSynth synthesizer allocation failed");
  require(fluid_synth_set_interp_method(synth.get(), -1, FLUID_INTERP_7THORDER), "interpolation");

  ProgramBinding binding;
  binding.channel_font = fonts.channel_font;
  binding.trace = &trace;
  for (const SoundfontInfo& font : fonts.fonts) {
    const int id = load_font(synth.get(), font.path);
    binding.sfont_ids.push_back(id);
    binding.presets.push_back(preset_keys(read_presets(font_of(synth.get(), id))));
  }

  std::array<ChannelSends, 16> sends{};
  for (int channel = 0; channel < 16; ++channel) {
    select_program(synth.get(), binding, channel, 0);
    sends[channel] = initial_sends(options.reverb.channel_send, channel);
    apply_sends(synth.get(), channel, sends[channel]);
  }

  const double rate = static_cast<double>(kSampleRate);
  const std::size_t total = sum.frames();
  std::size_t position = 0;
  std::array<float, kBlockFrames> left{};
  std::array<float, kBlockFrames> right{};

  const auto render_until = [&](std::size_t target) {
    target = std::min(target, total);
    while (position < target) {
      const std::size_t count = std::min(kBlockFrames, target - position);
      if (fluid_synth_write_float(synth.get(), static_cast<int>(count), left.data(), 0, 1,
                                  right.data(), 0, 1) != FLUID_OK) {
        throw Error("synth_failed", "FluidSynth rendering failed");
      }
      for (std::size_t i = 0; i < count; ++i) {
        sum.channels[0][position + i] += gain * left[i];
        sum.channels[1][position + i] += gain * right[i];
      }
      position += count;
    }
  };

  for (std::size_t i = 0; i < sequence.events.size(); ++i) {
    if ((plan.masks[i] & bit) == 0) continue;
    const MidiEvent& event = sequence.events[i];
    render_until(static_cast<std::size_t>(std::llround(event.seconds * rate)));
    dispatch(synth.get(), binding, options.reverb.channel_send, sends, event);
  }
  render_until(total);
}

}  // namespace

std::vector<PresetInfo> list_font_presets(const fs::path& path) {
  const SettingsPtr settings = make_settings(ReverbOptions{});
  SynthPtr synth(new_fluid_synth(settings.get()));
  if (!synth) throw Error("synth_failed", "FluidSynth synthesizer allocation failed");
  const int id = load_font(synth.get(), path);
  return read_presets(font_of(synth.get(), id));
}

Audio render_sequence(const MidiSequence& sequence, const SoundfontInfo& primary,
                      const RenderOptions& options, RenderReport* report) {
  const double rate = static_cast<double>(kSampleRate);
  const auto tail = static_cast<std::size_t>(kTailSeconds * rate);
  const auto total = static_cast<std::size_t>(std::ceil(sequence.duration_s * rate)) + tail;

  const Plan plan = plan_events(sequence, options);

  // 同一字体路径只载入一次;各通道指向其所属字体。
  FontSet fonts;
  fonts.fonts.push_back(primary);
  for (const FontOverride& item : options.font_overrides) {
    const auto found = std::find_if(fonts.fonts.begin(), fonts.fonts.end(),
                                    [&](const SoundfontInfo& font) {
                                      return font.path == item.font.path;
                                    });
    int index = 0;
    if (found == fonts.fonts.end()) {
      fonts.fonts.push_back(item.font);
      index = static_cast<int>(fonts.fonts.size()) - 1;
    } else {
      index = static_cast<int>(found - fonts.fonts.begin());
    }
    fonts.channel_font[item.channel] = index;
  }

  SelectTrace trace;
  trace.missing.resize(fonts.fonts.size());

  Audio audio;
  audio.rate = kSampleRate;
  audio.channels.assign(2, std::vector<float>(total, 0.0F));

  for (std::size_t s = 0; s < kStemCount; ++s) {
    const auto stem = static_cast<Stem>(s);
    if ((plan.used & stem_bit(stem)) == 0) continue;
    const double gain_db = stem_gain_db(stem, options.balance);
    const auto gain = static_cast<float>(std::pow(10.0, gain_db / 20.0));
    render_stem(sequence, plan, stem_bit(stem), gain, fonts, options, audio, trace);
    if (report) {
      report->stems.push_back(
          {stem_name(stem), options.balance ? stem_offset_db(stem) : 0.0, gain_db});
    }
  }

  if (report) {
    report->channels.clear();
    for (int channel = 0; channel < 16; ++channel) {
      const ChannelSends& state = plan.channels[channel];
      if (!state.used) continue;
      report->channels.push_back({channel + 1, state.program, state.reverb,
                                  std::string(send_source_name(state.reverb_source)),
                                  state.chorus,
                                  std::string(send_source_name(state.chorus_source))});
    }
    const SettingsPtr settings = make_settings(options.reverb);
    report->reverb_room = read_setting(settings, "synth.reverb.room-size");
    report->reverb_level = read_setting(settings, "synth.reverb.level");

    report->fonts.clear();
    report->fallbacks.clear();
    for (std::size_t f = 0; f < fonts.fonts.size(); ++f) {
      FontReport font;
      font.id = fonts.fonts[f].id;
      font.file = fonts.fonts[f].file_name;
      for (int channel = 0; channel < 16; ++channel) {
        if (!plan.channels[channel].used || fonts.channel_font[channel] != static_cast<int>(f)) {
          continue;
        }
        font.channels.push_back(channel + 1);
      }
      for (const auto& [channel, wanted] : trace.missing[f]) {
        if (!plan.channels[channel].used) continue;
        const MissingPreset miss{channel + 1, wanted, kFallbackReason};
        font.missing_presets.push_back(miss);
        report->fallbacks.push_back(miss);
      }
      report->fonts.push_back(std::move(font));
    }
  }
  return audio;
}

}  // namespace cgmusic
