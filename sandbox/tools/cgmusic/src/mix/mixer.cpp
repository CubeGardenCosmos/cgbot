#include "mix/mixer.hpp"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstdio>
#include <string>
#include <utility>

#include "common.hpp"
#include "loudness.hpp"
#include "mix/dynamics.hpp"
#include "mix/reverb.hpp"

namespace cgmusic {
namespace {

constexpr double kCeilingDbtp = -1.5;   // 与 VideoMaker loudnorm TP=-1.5 一致。
// VideoMaker 无目标响度分支:alimiter=limit=0.95,即 20*log10(0.95) = -0.4455 dBTP。
constexpr double kLimiterCeilingDbtp = -0.4455;
constexpr double kMaxReverbLevel = 1.5;  // VideoMaker estimate_reverb_wetness 的上限。
constexpr int kMaxSend = 127;
constexpr int kOtherFamilySend = 30;

struct FamilySend {
  const char* family;
  int send;
};

// 族默认混响发送。cgmusic 自行调校(VideoMaker 无按族的发送表);鼓组沿用 sends.cpp 的 15。
constexpr FamilySend kFamilySends[] = {
    {"strings", 40},  {"woodwinds", 30}, {"brass", 25},     {"guitars", 25},  {"keys", 35},
    {"harp", 45},     {"choir", 55},     {"handbells", 50}, {"percussion", 15}, {"drumline", 15},
};

struct FamilyStem {
  const char* family;
  Stem stem;
};

constexpr FamilyStem kFamilyStems[] = {
    {"woodwinds", Stem::Woodwind},  {"woodwind", Stem::Woodwind},
    {"brass", Stem::Brass},         {"accordion", Stem::Accordion},
    {"percussion", Stem::OtherDrum}, {"drumline", Stem::OtherDrum},
};

std::string lowercase(std::string_view text) {
  std::string result(text);
  std::transform(result.begin(), result.end(), result.begin(),
                 [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
  return result;
}

std::string number(double value) {
  char buffer[64];
  std::snprintf(buffer, sizeof buffer, "%.4f", value);
  return buffer;
}

bool all_finite(const std::vector<float>& samples) {
  return std::all_of(samples.begin(), samples.end(),
                     [](float value) { return std::isfinite(value); });
}

}  // namespace

int default_reverb_send(std::string_view family) noexcept {
  const std::string key = lowercase(family);
  for (const auto& entry : kFamilySends) {
    if (key == entry.family) return entry.send;
  }
  return kOtherFamilySend;
}

Stem stem_for_family(std::string_view family) noexcept {
  const std::string key = lowercase(family);
  for (const auto& entry : kFamilyStems) {
    if (key == entry.family) return entry.stem;
  }
  return Stem::Melodic;
}

MixResult mix_stems(const std::vector<MixInput>& inputs, const MixOptions& options) {
  if (inputs.empty()) throw Error("bad_input", "No stems to mix");
  if (!(options.reverb_room >= 0.0 && options.reverb_room <= 1.0)) {
    throw Error("bad_input", "reverb room must be 0-1");
  }
  if (!(options.reverb_level >= 0.0 && options.reverb_level <= kMaxReverbLevel)) {
    throw Error("bad_input", "reverb level must be 0-1.5");
  }
  if (options.target_lufs && !std::isfinite(*options.target_lufs)) {
    throw Error("bad_input", "target LUFS must be finite");
  }

  std::size_t frames = 0;
  for (const MixInput& input : inputs) {
    if (input.left.size() != input.right.size()) {
      throw Error("bad_input", "Stem channels differ in length");
    }
    if (input.reverb_send && (*input.reverb_send < 0 || *input.reverb_send > kMaxSend)) {
      throw Error("bad_input", "reverb send must be 0-127");
    }
    if (!all_finite(input.left) || !all_finite(input.right)) {
      throw Error("bad_input", "Stem audio is not finite");
    }
    frames = std::max(frames, input.left.size());
  }
  if (frames == 0) throw Error("bad_input", "No audio to mix");

  const auto rate = static_cast<double>(kSampleRate);
  const bool balance = options.auto_balance;

  // 干声与总线分配在此处一次完成;循环内只做乘加。
  std::vector<float> dry_l(frames, 0.0F);
  std::vector<float> dry_r(frames, 0.0F);
  std::vector<float> bus_l(frames, 0.0F);
  std::vector<float> bus_r(frames, 0.0F);

  std::string stems_json;
  for (std::size_t i = 0; i < inputs.size(); ++i) {
    const MixInput& input = inputs[i];
    const Stem stem = stem_for_family(input.family);
    const double gain_db = stem_gain_db(stem, balance);
    const auto gain = static_cast<float>(std::pow(10.0, gain_db / 20.0));
    const bool explicit_send = input.reverb_send.has_value();
    const int send = explicit_send ? *input.reverb_send : default_reverb_send(input.family);
    // 发送后于声部电平(post-fader):总线 = 增益 * 发送比例 * 声部。
    const auto send_gain = static_cast<float>(gain * send / static_cast<double>(kMaxSend));

    for (std::size_t n = 0; n < input.left.size(); ++n) {
      dry_l[n] += input.left[n] * gain;
      dry_r[n] += input.right[n] * gain;
      bus_l[n] += input.left[n] * send_gain;
      bus_r[n] += input.right[n] * send_gain;
    }

    if (i != 0) stems_json += ",";
    stems_json += "{\"family\":" + json_string(input.family) +
                  ",\"channel\":" + std::to_string(input.channel) +
                  ",\"stem\":" + json_string(stem_name(stem)) +
                  ",\"gain_db\":" + number(gain_db) + ",\"send\":" + std::to_string(send) +
                  ",\"send_source\":" + json_string(explicit_send ? "explicit" : "default") + "}";
  }

  // 湿声:总线经 VideoMaker 预设(HP 150 -> Freeverb -> LP 8.5k),电平由 reverb_level 缩放。
  std::vector<float> wet_l(frames, 0.0F);
  std::vector<float> wet_r(frames, 0.0F);
  if (options.reverb_level > 0.0) {
    AuxReverb reverb(rate, options.reverb_room, kVideoMakerDamping, kVideoMakerWidth);
    const auto level = static_cast<float>(options.reverb_level);
    for (std::size_t n = 0; n < frames; ++n) {
      reverb.process(bus_l[n], bus_r[n], wet_l[n], wet_r[n]);
      wet_l[n] *= level;
      wet_r[n] *= level;
    }
  }

  // 干声 + 湿声写入输出缓冲(复用干声缓冲,不再分配)。
  Audio audio;
  audio.rate = kSampleRate;
  audio.channels.resize(2);
  audio.channels[0] = std::move(dry_l);
  audio.channels[1] = std::move(dry_r);
  for (std::size_t n = 0; n < frames; ++n) {
    audio.channels[0][n] += wet_l[n];
    audio.channels[1][n] += wet_r[n];
  }

  // 响度与真峰值:target 为 nullopt 时保持混音电平,仅施加真峰值上限。
  const double target = options.target_lufs ? *options.target_lufs : measure(audio).lufs;
  const double ceiling = options.target_lufs ? kCeilingDbtp : kLimiterCeilingDbtp;
  std::optional<DynamicsReport> dynamics;
  Quality quality;
  if (options.master_dynamics) {
    dynamics = compress_master(audio, target, ceiling, DynamicsSettings{}, &quality);
  } else {
    quality = master(audio, target, ceiling);
  }

  std::string dynamics_json;
  if (dynamics) {
    dynamics_json = ",\"dynamics\":{\"threshold_db\":" + number(dynamics->threshold_db) +
                    ",\"ratio\":" + number(dynamics->ratio) +
                    ",\"max_gain_reduction_db\":" + number(dynamics->max_gain_reduction_db) +
                    ",\"avg_gain_reduction_db\":" + number(dynamics->average_gain_reduction_db) +
                    ",\"makeup_db\":" + number(dynamics->makeup_db) + "}";
  }

  MixResult result;
  result.left = std::move(audio.channels[0]);
  result.right = std::move(audio.channels[1]);
  result.report =
      "{\"engine\":\"cgmusic-mix\",\"balance\":" + std::string(balance ? "true" : "false") +
      ",\"reverb\":{\"room\":" + number(options.reverb_room) +
      ",\"level\":" + number(options.reverb_level) + ",\"damping\":" + number(kVideoMakerDamping) +
      ",\"width\":" + number(kVideoMakerWidth) + ",\"stems\":[" + stems_json + "]}" +
      ",\"loudness\":{\"target_lufs\":" + number(target) + ",\"lufs\":" + number(quality.lufs) +
      ",\"true_peak_dbtp\":" + number(quality.true_peak_dbtp) +
      ",\"lra\":" + number(quality.lra) + ",\"clipped\":" +
      std::string(quality.clipped ? "true" : "false") + dynamics_json + "}}";
  return result;
}

}  // namespace cgmusic
