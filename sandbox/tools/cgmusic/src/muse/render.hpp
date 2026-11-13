#pragma once

#include <cstddef>
#include <functional>
#include <string>
#include <vector>

#include "midi.hpp"
#include "sends.hpp"
#include "mix/mixer.hpp"
#include "muse/catalog.hpp"
#include "muse/loader.hpp"
#include "../render.hpp"

namespace cgmusic::muse {

// 已绑定 ABI 的纯事件层。音符开关节奏、音色与力度都在此固定,便于无真实库测试。
struct ChannelPlan {
  int channel = 0;  // 索引 0-15
  int program = 0;
  std::optional<int> instrument_id;
  std::string instrument_name;
  std::string family;
  ChannelSends sends;
  std::vector<abi::ms_NoteEvent_5> notes;
  std::vector<abi::ms_DynamicsEvent_2> dynamics;
};

std::uint64_t articulation_for_program(int program) noexcept;

// 每个发声 MIDI 通道一条计划;PC 取最后一次值,力度取最后一个非零 note-on。
// 没有对应已许可 Muse 乐器的通道 instrument_id 为 nullopt (回退到 FluidSynth);
// 若全部发声通道都需回退,则抛出 muse_unavailable。
std::string family_for_gm(int program, bool is_drum) noexcept;
std::vector<ChannelPlan> plan_channels(const MidiSequence& sequence,
                                       const RenderOptions& options, const Catalog& catalog);

class SamplerSession {
 public:
  explicit SamplerSession(const Api& api);
  ~SamplerSession();
  SamplerSession(const SamplerSession&) = delete;
  SamplerSession& operator=(const SamplerSession&) = delete;

  // init_2 -> add_track -> events -> finalize_track -> trigger_render/ready -> offline。
  void add_score(const std::vector<ChannelPlan>& channels);
  // 输出非交错双声道 float,包含调用方指定的帧数。
  Audio render(std::size_t frames);

 private:
  const Api& api_;
  abi::ms_MuseSampler sampler_ = nullptr;
  std::size_t tracks_ = 0;
  bool ready_ = false;
  bool offline_ = false;
};

struct DryRender {
  std::vector<Audio> stems;  // 与 plans 同下标,每条只包含一个通道
  double seconds = 0.0;
};
DryRender render_dry(const MuseLib& lib, const std::vector<ChannelPlan>& plans, std::size_t frames);

// 混音入口独立于动态库,便于用真实平面 PCM 验证响度与真峰值处理。
Audio mix_rendered(const DryRender& dry, const std::vector<ChannelPlan>& plans,
                   const RenderOptions& options, double target_lufs, MuseRenderResult* report);

// 混合 Muse 干声轨道与 FluidSynth 回退干声轨道
Audio mix_hybrid(const DryRender& dry, const std::vector<ChannelPlan>& muse_plans,
                 const std::vector<Audio>& fallback_stems,
                 const std::vector<ChannelPlan>& fallback_plans,
                 const RenderOptions& options, double target_lufs, MuseRenderResult* report);

Audio render_sequence_muse(const MidiSequence& sequence, const RenderOptions& options,
                           double target_lufs, MuseRenderResult* report);

}  // namespace cgmusic::muse
