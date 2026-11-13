#pragma once

#include <cstddef>
#include <optional>
#include <string>
#include <vector>

#include "midi.hpp"
#include "muse/catalog.hpp"
#include "muse/loader.hpp"
#include "../render.hpp"

namespace cgmusic::muse {

// 已绑定 ABI 的纯事件层：音符开关、音色、力度与关节。
struct ChannelPlan {
  int channel = 0;
  int program = 0;
  std::optional<int> instrument_id;
  std::string instrument_name;
  std::string family;
  std::vector<abi::ms_NoteEvent_5> notes;
  std::vector<abi::ms_DynamicsEvent_2> dynamics;
};

std::uint64_t articulation_for_program(int program) noexcept;
std::string family_for_gm(int program, bool is_drum) noexcept;

// 为序列中实际发声的通道规划事件；未映射乐器由上层显式回退。
std::vector<ChannelPlan> plan_channels(const MidiSequence& sequence, const Catalog& catalog);

class SamplerSession {
 public:
  explicit SamplerSession(const Api& api);
  ~SamplerSession();
  SamplerSession(const SamplerSession&) = delete;
  SamplerSession& operator=(const SamplerSession&) = delete;

  void add_score(const std::vector<ChannelPlan>& channels);
  Audio render(std::size_t frames);

 private:
  const Api& api_;
  abi::ms_MuseSampler sampler_ = nullptr;
  bool offline_ = false;
};

struct DryRender {
  std::vector<Audio> stems;
  double seconds = 0.0;
};
DryRender render_dry(const MuseLib& lib, const std::vector<ChannelPlan>& plans, std::size_t frames);

}  // namespace cgmusic::muse
