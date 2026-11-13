#include "muse/render.hpp"

#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <cstdlib>
#include <limits>
#include <string>
#include <thread>
#include <utility>
#include <vector>

#include "muse/mapping.hpp"

namespace cgmusic::muse {
namespace {

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
  if (result != abi::kResultOk) api_failed(function, result);
}

struct ChannelState {
  bool used = false;
  int program = 0;
  int velocity = 100;
};

std::vector<ChannelState> channel_states(const MidiSequence& sequence, std::size_t count) {
  std::vector<ChannelState> states(count);
  for (const MidiEvent& event : sequence.events) {
    const int channel = event.channel;
    if (channel < 0 || static_cast<std::size_t>(channel) >= count) continue;
    if ((event.status & 0xF0) == 0xC0) states[channel].program = event.data1;
    if ((event.status & 0xF0) == 0x90 && event.data2 != 0) {
      states[channel].used = true;
      states[channel].velocity = event.data2;
    }
  }
  return states;
}

struct OpenNote {
  double start = 0.0;
  double tempo = 120.0;
};

void close_note(ChannelPlan& plan, std::array<std::vector<OpenNote>, 128>& open, int pitch,
                double end) {
  auto& notes = open[static_cast<std::size_t>(pitch)];
  if (notes.empty()) return;
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

bool audible(const Audio& audio) {
  return std::any_of(audio.channels.begin(), audio.channels.end(), [](const auto& channel) {
    return std::any_of(channel.begin(), channel.end(), [](float sample) {
      return std::fabs(sample) > 1e-7F;
    });
  });
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

std::vector<ChannelPlan> plan_channels(const MidiSequence& sequence, const Catalog& catalog) {
  std::size_t count = 16;
  for (const MidiEvent& event : sequence.events) {
    if (event.channel >= 0) count = std::max(count, static_cast<std::size_t>(event.channel + 1));
  }
  const std::vector<ChannelState> states = channel_states(sequence, count);
  std::vector<ChannelPlan> plans(count);
  for (std::size_t channel = 0; channel < count; ++channel) {
    ChannelPlan& plan = plans[channel];
    plan.channel = static_cast<int>(channel);
    plan.program = states[channel].program;
    if (!states[channel].used) continue;
    const bool is_drum = channel < sequence.channel_is_drum.size()
                             ? sequence.channel_is_drum[channel]
                             : channel == static_cast<std::size_t>(kDrumChannel);
    const std::string name = channel < sequence.channel_names.size()
                                 ? sequence.channel_names[channel]
                                 : std::string();
    plan.instrument_id = find_exact_instrument(catalog, name);
    if (!plan.instrument_id) plan.instrument_id = pick_instrument(plan.program, is_drum, catalog);
    if (plan.instrument_id) {
      const MuseInstrument& instrument = find_instrument(catalog, *plan.instrument_id);
      plan.instrument_name = instrument.name;
      plan.family = instrument.family;
    } else {
      plan.family = family_for_gm(plan.program, is_drum);
    }
  }

  std::vector<std::array<std::vector<OpenNote>, 128>> open(count);
  for (const MidiEvent& event : sequence.events) {
    const int channel = event.channel;
    if (channel < 0 || static_cast<std::size_t>(channel) >= count || !states[channel].used) continue;
    ChannelPlan& plan = plans[channel];
    const int pitch = event.data1;
    switch (event.status & 0xF0) {
      case 0x80:
        close_note(plan, open[channel], pitch, event.seconds);
        break;
      case 0x90:
        if (event.data2 == 0) close_note(plan, open[channel], pitch, event.seconds);
        else open[channel][static_cast<std::size_t>(pitch)].push_back(
            {event.seconds, midi_tempo_at(sequence, event.seconds).bpm()});
        break;
      default:
        break;
    }
  }
  for (std::size_t channel = 0; channel < count; ++channel) {
    if (!states[channel].used) continue;
    for (int pitch = 0; pitch < 128; ++pitch) {
      while (!open[channel][static_cast<std::size_t>(pitch)].empty()) {
        close_note(plans[channel], open[channel], pitch, sequence.duration_s);
      }
    }
    plans[channel].dynamics.push_back({0, states[channel].velocity / 127.0});
  }
  plans.erase(std::remove_if(plans.begin(), plans.end(),
                             [](const ChannelPlan& plan) { return plan.notes.empty(); }),
              plans.end());
  if (plans.empty()) throw Error("muse_unavailable", "Muse 输入没有任何可渲染音符");
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
    try { api_.ms_MuseSampler_destroy(sampler_); } catch (...) {}
  }
}

void SamplerSession::add_score(const std::vector<ChannelPlan>& channels) {
  for (const ChannelPlan& channel : channels) {
    if (!channel.instrument_id) throw Error("muse_unavailable", "无法为未映射乐器的通道添加轨道");
    const abi::ms_Track track = abi_call("ms_MuseSampler_add_track", [&] {
      return api_.ms_MuseSampler_add_track(sampler_, *channel.instrument_id);
    });
    if (track == nullptr) throw Error("muse_unavailable", "Muse 无法创建乐器轨道");
    for (const auto& dynamics : channel.dynamics) {
      require_abi("ms_MuseSampler_add_track_dynamics_event_2", [&] {
        return api_.ms_MuseSampler_add_track_dynamics_event_2(sampler_, track, dynamics);
      });
    }
    for (const auto& note : channel.notes) {
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
  require_abi("ms_MuseSampler_start_offline_mode", [&] {
    return api_.ms_MuseSampler_start_offline_mode(sampler_, static_cast<double>(kSampleRate));
  });
  offline_ = true;

  Audio audio;
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
      std::copy_n(block[channel].begin(), count,
                  audio.channels[channel].begin() + static_cast<std::ptrdiff_t>(done));
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

Audio render_sequence_muse(const MidiSequence& sequence, MuseRenderResult* report) {
  const std::string samples = env_or("MUSE_SAMPLES_DIR", kDefaultSamples);
  const Catalog full_catalog = load_catalog(samples);
  if (full_catalog.empty()) throw Error("muse_unavailable", "Muse 样本目录中没有已安装乐器:" + samples);
  const MuseLib lib = MuseLib::load(env_or("MUSESAMPLER_LIB", kDefaultLibrary));
  lib.init(samples);
  const Catalog accepted = intersect_catalog(full_catalog, lib.list_instruments());
  const std::vector<ChannelPlan> plans = plan_channels(sequence, accepted);
  const std::size_t frames = static_cast<std::size_t>(std::ceil(sequence.duration_s * kSampleRate)) +
                             kTailFrames;

  std::vector<ChannelPlan> muse_plans;
  for (const ChannelPlan& plan : plans) {
    if (plan.instrument_id) {
      muse_plans.push_back(plan);
    } else if (report) {
      report->fallbacks.push_back({plan.channel + 1,
                                   PresetKey{plan.channel == kDrumChannel ? 128 : 0, plan.program},
                                   "muse_instrument_not_licensed"});
    }
  }
  if (muse_plans.empty()) {
    throw Error("muse_unavailable", "Muse 没有可用的已许可乐器;请使用 --engine fluidsynth");
  }
  const DryRender dry = render_dry(lib, muse_plans, frames);
  for (const Audio& stem : dry.stems) {
    if (!audible(stem)) throw Error("muse_unavailable", "MuseSampler 输出全静音");
  }
  if (report) {
    report->version = lib.version().text;
    report->dry_seconds = dry.seconds;
    for (std::size_t i = 0; i < muse_plans.size(); ++i) {
      const ChannelPlan& plan = muse_plans[i];
      report->tracks.push_back({plan.channel + 1, plan.program, *plan.instrument_id,
                                plan.instrument_name, plan.family, plan.notes.size()});
    }
  }
  // 单声部输入只有一条轨道；保留通用计划的检查，禁止意外混合多声部。
  if (dry.stems.size() != 1) {
    throw Error("bad_input", "cgsynth render expects a single-part MIDI file");
  }
  return std::move(dry.stems.front());
}

}  // namespace cgmusic::muse

namespace cgmusic {
Audio render_sequence_muse(const MidiSequence& sequence, MuseRenderResult* report) {
  return muse::render_sequence_muse(sequence, report);
}
}  // namespace cgmusic
