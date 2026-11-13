#include "muse/render.hpp"
#include "muse/mapping.hpp"
#include "soundfont.hpp"

#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <cstdlib>
#include <filesystem>
#include <limits>
#include <memory>
#include <optional>
#include <string>
#include <thread>
#include <utility>

#include "common.hpp"

namespace cgmusic::muse {
namespace {
namespace fs = std::filesystem;


constexpr int kDrumChannel = 9;
constexpr std::size_t kTailFrames = 4 * kSampleRate;
constexpr int kBlockFrames = 512;
constexpr auto kReadyTimeout = std::chrono::minutes(10);
constexpr const char* kDefaultLibrary = "/opt/musesampler/lib/libMuseSamplerCoreLib.so";
constexpr const char* kDefaultSamples = "/opt/muse-sounds";

long long microseconds(double seconds) {
  const double value = seconds * 1'000'000.0;
  if (!(value >= 0.0 && value <= static_cast<double>(std::numeric_limits<long long>::max()))) {
    throw Error("bad_input", "Muse event time is out of range");
  }
  return static_cast<long long>(std::llround(value));
}

std::string env_or(const char* name, const char* fallback) {
  const char* value = std::getenv(name);
  return value != nullptr && *value != '\0' ? value : fallback;
}

const MuseInstrument& find_instrument(const Catalog& catalog, int id) {
  const auto it = std::find_if(catalog.begin(), catalog.end(),
                               [&](const MuseInstrument& item) { return item.id == id; });
  if (it == catalog.end()) throw Error("muse_unavailable", "Muse 乐器目录内部不一致");
  return *it;
}

[[noreturn]] void api_failed(const char* function, abi::ms_Result result) {
  throw Error("muse_unavailable",
              std::string("MuseSampler ") + function + " 失败,返回 " + std::to_string(result));
}

template <typename Fn>
auto abi_call(const char* function, Fn&& fn) -> decltype(fn()) {
  try {
    return fn();
  } catch (const std::exception& e) {
    throw Error("muse_unavailable",
                std::string("MuseSampler ") + function + " 抛出异常: " + e.what());
  } catch (...) {
    throw Error("muse_unavailable",
                std::string("MuseSampler ") + function + " 抛出未知异常");
  }
}

template <typename Fn>
void require_abi(const char* function, Fn&& fn) {
  const abi::ms_Result result = abi_call(function, std::forward<Fn>(fn));
  if (result != abi::kResultOk) {
    api_failed(function, result);
  }
}


// 保存每个发声通道的最后一次非零力度与程序号。关闭音符不覆盖力度。
struct ChannelState {
  bool used = false;
  int program = 0;
  int velocity = 100;
};

std::vector<ChannelState> channel_states(const MidiSequence& sequence) {
  std::vector<ChannelState> states(16);
  for (const MidiEvent& event : sequence.events) {
    const int channel = event.status & 0x0F;
    switch (event.status & 0xF0) {
      case 0xC0:
        states[channel].program = event.data1;
        break;
      case 0x90:
        if (event.data2 != 0) {
          states[channel].used = true;
          states[channel].velocity = event.data2;
        }
        break;
      default:
        break;
    }
  }
  return states;
}

// 通道内每个 pitch 的栈。SMF 允许同音高重叠;栈按后开先关配对,保证不丢失音符。
struct OpenNote {
  double start = 0.0;
  double tempo = 120.0;
};

void close_note(ChannelPlan& plan, std::array<std::vector<OpenNote>, 128>& open, int pitch,
                double end) {
  std::vector<OpenNote>& notes = open[static_cast<std::size_t>(pitch)];
  if (notes.empty()) return;  // 与 FluidSynth 一致:没有对应 note-on 的 note-off 不产生事件。
  const OpenNote note = notes.back();
  notes.pop_back();
  if (!(end > note.start)) return;

  abi::ms_NoteEvent_5 event{};
  event._voice = 0;
  event._articulation = articulation_for_program(plan.program);
  event._location_us = microseconds(note.start);
  event._duration_us = microseconds(end - note.start);
  event._pitch = pitch;
  event._tempo = note.tempo;
  plan.notes.push_back(event);
}

}  // namespace

std::uint64_t articulation_for_program(int program) noexcept {
  return program == 45 ? abi::kArticulationPizzicato : abi::kArticulationNone;
}

std::string family_for_gm(int program, bool is_drum) noexcept {
  if (is_drum) return "percussion";
  if (program >= 40 && program <= 51) return "strings";
  if (program >= 52 && program <= 55) return "choir";
  if (program >= 56 && program <= 63) return "brass";
  if (program >= 64 && program <= 79) return "woodwinds";
  if (program >= 24 && program <= 39) return "guitars";
  if (program >= 0 && program <= 23) return "keys";
  return "other";
}

std::vector<ChannelPlan> plan_channels(const MidiSequence& sequence,
                                       const RenderOptions& options, const Catalog& catalog) {
  const std::vector<ChannelState> states = channel_states(sequence);
  std::vector<ChannelPlan> plans(16);
  for (int channel = 0; channel < 16; ++channel) {
    ChannelPlan& plan = plans[channel];
    plan.channel = channel;
    plan.program = states[channel].program;
    plan.sends = initial_sends(options.reverb.channel_send, channel);
    if (!states[channel].used) continue;
    plan.instrument_id = pick_instrument(plan.program, channel == kDrumChannel, catalog);
    if (plan.instrument_id) {
      const MuseInstrument& instrument = find_instrument(catalog, *plan.instrument_id);
      plan.instrument_name = instrument.name;
      plan.family = instrument.family;
    } else {
      plan.instrument_name = "";
      plan.family = family_for_gm(plan.program, channel == kDrumChannel);
    }
    plan.notes.reserve(256);
  }

  std::array<std::array<std::vector<OpenNote>, 128>, 16> open;
  for (const MidiEvent& event : sequence.events) {
    const int channel = event.status & 0x0F;
    ChannelPlan& plan = plans[channel];
    update_sends(options.reverb.channel_send, channel, event, plan.sends);
    if (!states[channel].used) continue;
    const int pitch = event.data1;
    switch (event.status & 0xF0) {
      case 0x80:
        close_note(plan, open[channel], pitch, event.seconds);
        break;
      case 0x90:
        if (event.data2 == 0) {
          close_note(plan, open[channel], pitch, event.seconds);
        } else {
          open[channel][static_cast<std::size_t>(pitch)].push_back(
              {event.seconds, midi_tempo_at(sequence, event.seconds).bpm()});
        }
        break;
      default:
        break;
    }
  }
  for (int channel = 0; channel < 16; ++channel) {
    if (!states[channel].used) continue;
    for (int pitch = 0; pitch < 128; ++pitch) {
      while (!open[channel][static_cast<std::size_t>(pitch)].empty()) {
        close_note(plans[channel], open[channel], pitch, sequence.duration_s);
      }
    }
    const double velocity = states[channel].velocity / 127.0;
    plans[channel].dynamics.push_back({0, velocity});
    plans[channel].sends.used = true;
  }

  plans.erase(std::remove_if(plans.begin(), plans.end(),
                             [](const ChannelPlan& plan) { return plan.notes.empty(); }),
              plans.end());
  if (plans.empty()) throw Error("muse_unavailable", "Muse 输入没有任何可渲染音符");

  const bool any_muse = std::any_of(
      plans.begin(), plans.end(), [](const ChannelPlan& plan) { return plan.instrument_id.has_value(); });
  if (!any_muse) {
    throw Error("muse_unavailable",
                "Muse 没有可用的已许可乐器;全部通道均需回退,请使用 --engine fluidsynth");
  }
  return plans;
}

SamplerSession::SamplerSession(const Api& api) : api_(api) {
  sampler_ = abi_call("ms_MuseSampler_create", [&] { return api_.ms_MuseSampler_create(); });
  if (sampler_ == nullptr) throw Error("muse_unavailable", "ms_MuseSampler_create 返回空");
  require_abi("ms_MuseSampler_init_2", [&] {
    return api_.ms_MuseSampler_init_2(sampler_, static_cast<double>(kSampleRate), kBlockFrames, 2);
  });
}

SamplerSession::~SamplerSession() {
  if (sampler_ != nullptr) {
    try {
      api_.ms_MuseSampler_destroy(sampler_);
    } catch (...) {
      // Destructors must never throw
    }
  }
}

void SamplerSession::add_score(const std::vector<ChannelPlan>& channels) {
  for (const ChannelPlan& channel : channels) {
    if (!channel.instrument_id) {
      throw Error("muse_unavailable", "无法为未映射乐器的通道添加 MuseSampler 轨道");
    }
    const abi::ms_Track track = abi_call("ms_MuseSampler_add_track", [&] {
      return api_.ms_MuseSampler_add_track(sampler_, *channel.instrument_id);
    });
    if (track == nullptr) {
      throw Error("muse_unavailable",
                  "Muse 无法创建通道 " + std::to_string(channel.channel + 1) + " 的乐器轨道 " +
                      std::to_string(*channel.instrument_id));
    }
    ++tracks_;
    for (const abi::ms_DynamicsEvent_2& dynamics : channel.dynamics) {
      require_abi("ms_MuseSampler_add_track_dynamics_event_2", [&] {
        return api_.ms_MuseSampler_add_track_dynamics_event_2(sampler_, track, dynamics);
      });
    }
    for (const abi::ms_NoteEvent_5& note : channel.notes) {
      long long event_id = -1;
      require_abi("ms_MuseSampler_add_track_note_event_6", [&] {
        return api_.ms_MuseSampler_add_track_note_event_6(sampler_, track, note, &event_id);
      });
    }
    require_abi("ms_MuseSampler_finalize_track", [&] {
      return api_.ms_MuseSampler_finalize_track(sampler_, track);
    });
  }
}

Audio SamplerSession::render(std::size_t frames) {
  abi_call("ms_MuseSampler_trigger_render", [&] {
    api_.ms_MuseSampler_trigger_render(sampler_);
    return 0;
  });
  const auto deadline = std::chrono::steady_clock::now() + kReadyTimeout;
  while (!abi_call("ms_MuseSampler_ready_to_play", [&] {
    return api_.ms_MuseSampler_ready_to_play(sampler_);
  })) {
    if (std::chrono::steady_clock::now() >= deadline) {
      throw Error("muse_unavailable", "MuseSampler 等待 ready_to_play 超过 10 分钟");
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(20));
  }
  ready_ = true;
  require_abi("ms_MuseSampler_start_offline_mode", [&] {
    return api_.ms_MuseSampler_start_offline_mode(sampler_, static_cast<double>(kSampleRate));
  });
  offline_ = true;

  Audio audio;
  audio.rate = kSampleRate;
  audio.channels.assign(2, std::vector<float>(frames, 0.0F));
  std::array<std::vector<float>, 2> block{
      std::vector<float>(kBlockFrames, 0.0F), std::vector<float>(kBlockFrames, 0.0F)};
  std::array<float*, 2> pointers{block[0].data(), block[1].data()};
  for (std::size_t done = 0; done < frames; done += kBlockFrames) {
    for (auto& channel : block) std::fill(channel.begin(), channel.end(), 0.0F);
    require_abi("ms_MuseSampler_process_offline", [&] {
      return api_.ms_MuseSampler_process_offline(sampler_, {pointers.data(), kBlockFrames, 2});
    });
    const std::size_t count = std::min<std::size_t>(kBlockFrames, frames - done);
    for (std::size_t channel = 0; channel < 2; ++channel) {
      std::copy_n(block[channel].begin(), count, audio.channels[channel].begin() +
                                                    static_cast<std::ptrdiff_t>(done));
    }
  }
  require_abi("ms_MuseSampler_stop_offline_mode", [&] {
    return api_.ms_MuseSampler_stop_offline_mode(sampler_);
  });
  offline_ = false;
  return audio;
}

DryRender render_dry(const MuseLib& lib, const std::vector<ChannelPlan>& plans,
                     std::size_t frames) {
  DryRender result;
  const auto start = std::chrono::steady_clock::now();
  result.stems.reserve(plans.size());
  for (const ChannelPlan& plan : plans) {
    SamplerSession session(lib.api());
    session.add_score({plan});
    result.stems.push_back(session.render(frames));
  }
  result.seconds = std::chrono::duration<double>(std::chrono::steady_clock::now() - start).count();
  return result;
}

Audio mix_hybrid(const DryRender& dry, const std::vector<ChannelPlan>& muse_plans,
                 const std::vector<Audio>& fallback_stems,
                 const std::vector<ChannelPlan>& fallback_plans,
                 const RenderOptions& options, double target_lufs, MuseRenderResult* report) {
  if (dry.stems.size() != muse_plans.size()) {
    throw Error("muse_unavailable", "MuseSampler 干声轨道数量不一致");
  }
  if (fallback_stems.size() != fallback_plans.size()) {
    throw Error("muse_unavailable", "FluidSynth 回退干声轨道数量不一致");
  }
  for (const Audio& stem : dry.stems) {
    if (stem.rate != kSampleRate || stem.channels.size() != 2) {
      throw Error("muse_unavailable", "MuseSampler 干声格式不是 44.1 kHz 双声道");
    }
  }
  for (const Audio& stem : fallback_stems) {
    if (stem.rate != kSampleRate || stem.channels.size() != 2) {
      throw Error("muse_unavailable", "FluidSynth 干声格式不是 44.1 kHz 双声道");
    }
  }

  const auto is_audio_audible = [](const Audio& a) {
    return std::any_of(a.channels.begin(), a.channels.end(), [](const std::vector<float>& channel) {
      return std::any_of(channel.begin(), channel.end(),
                         [](float sample) { return std::fabs(sample) > 1e-7F; });
    });
  };

  const bool audible = std::any_of(dry.stems.begin(), dry.stems.end(), is_audio_audible) ||
                       std::any_of(fallback_stems.begin(), fallback_stems.end(), is_audio_audible);
  if (!audible) throw Error("muse_unavailable", "MuseSampler 输出全静音");

  std::vector<MixInput> stems;
  stems.reserve(muse_plans.size() + fallback_plans.size());
  for (std::size_t i = 0; i < muse_plans.size(); ++i) {
    const ChannelPlan& plan = muse_plans[i];
    MixInput input;
    input.family = plan.family;
    input.channel = plan.channel + 1;
    input.left = dry.stems[i].channels[0];
    input.right = dry.stems[i].channels[1];
    input.reverb_send = plan.sends.reverb;
    stems.push_back(std::move(input));
  }
  for (std::size_t i = 0; i < fallback_plans.size(); ++i) {
    const ChannelPlan& plan = fallback_plans[i];
    MixInput input;
    input.family = plan.family;
    input.channel = plan.channel + 1;
    input.left = fallback_stems[i].channels[0];
    input.right = fallback_stems[i].channels[1];
    input.reverb_send = plan.sends.reverb;
    stems.push_back(std::move(input));
  }

  MixOptions mix;
  mix.reverb_room = options.reverb.room.value_or(0.75);
  mix.reverb_level = options.reverb.level.value_or(1.0);
  mix.target_lufs = target_lufs;
  mix.auto_balance = options.balance;
  const MixResult mixed = mix_stems(stems, mix);

  if (report) {
    report->reverb_room = mix.reverb_room;
    report->reverb_level = mix.reverb_level;
    report->dry_seconds = dry.seconds;
    report->mix_report = mixed.report;
    for (const ChannelPlan& plan : muse_plans) {
      report->tracks.push_back({plan.channel + 1, plan.program, *plan.instrument_id,
                                plan.instrument_name, plan.family, plan.notes.size()});
    }
  }
  Audio mastered;
  mastered.rate = kSampleRate;
  mastered.channels = {mixed.left, mixed.right};
  return mastered;
}

Audio mix_rendered(const DryRender& dry, const std::vector<ChannelPlan>& plans,
                   const RenderOptions& options, double target_lufs, MuseRenderResult* report) {
  return mix_hybrid(dry, plans, {}, {}, options, target_lufs, report);
}

Audio render_sequence_muse(const MidiSequence& sequence, const RenderOptions& options,
                           double target_lufs, MuseRenderResult* report) {
  const std::string samples = env_or("MUSE_SAMPLES_DIR", kDefaultSamples);
  const Catalog full_catalog = load_catalog(samples);
  if (full_catalog.empty()) {
    throw Error("muse_unavailable", "Muse 样本目录中没有已安装乐器:" + samples);
  }

  const MuseLib lib = MuseLib::load(env_or("MUSESAMPLER_LIB", kDefaultLibrary));
  lib.init(samples);
  const std::vector<InstrumentEntry> accepted = lib.list_instruments();
  const Catalog accepted_catalog = intersect_catalog(full_catalog, accepted);

  const std::vector<ChannelPlan> plans = plan_channels(sequence, options, accepted_catalog);

  std::vector<ChannelPlan> muse_plans;
  std::vector<ChannelPlan> fallback_plans;
  for (const ChannelPlan& plan : plans) {
    if (plan.instrument_id.has_value()) {
      muse_plans.push_back(plan);
    } else {
      fallback_plans.push_back(plan);
    }
  }

  const std::size_t frames = static_cast<std::size_t>(std::ceil(sequence.duration_s * kSampleRate)) +
                             kTailFrames;
  const DryRender dry = render_dry(lib, muse_plans, frames);

  std::vector<Audio> fallback_stems;
  if (!fallback_plans.empty()) {
    const fs::path dir = soundfont_dir();
    const SoundfontInfo font = default_soundfont(dir);
    for (const ChannelPlan& plan : fallback_plans) {
      fallback_stems.push_back(
          render_fluidsynth_channel(sequence, plan.channel, font, options, frames));
      if (report) {
        const int bank = plan.channel == kDrumChannel ? 128 : 0;
        report->fallbacks.push_back(
            {plan.channel + 1, PresetKey{bank, plan.program}, "muse_instrument_not_licensed"});
      }
    }
  }

  Audio mastered = mix_hybrid(dry, muse_plans, fallback_stems, fallback_plans, options, target_lufs, report);
  if (report) report->version = lib.version().text;
  return mastered;
}

}  // namespace cgmusic::muse

namespace cgmusic {

Audio render_sequence_muse(const MidiSequence& sequence, const RenderOptions& options,
                           double target_lufs, MuseRenderResult* report) {
  return muse::render_sequence_muse(sequence, options, target_lufs, report);
}

}  // namespace cgmusic
