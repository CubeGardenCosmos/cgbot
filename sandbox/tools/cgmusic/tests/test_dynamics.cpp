#include <algorithm>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <functional>
#include <memory>
#include <optional>
#include <string>
#include <vector>

#include <ebur128.h>

#include "common.hpp"
#include "loudness.hpp"
#include "mix/dynamics.hpp"
#include "mix/mixer.hpp"
#include "test_harness.hpp"

namespace cgmusic {
namespace {

constexpr double kPi = 3.14159265358979323846;
constexpr double kRate = 44100.0;

// 确定性、类管弦的动态素材:三个和声正弦加每秒一次的衰减瞬态,
// 段落电平按 dB 给出(安静 -> 响 -> 安静 -> 中等),峰均比约 10 dB 以上。
Audio dynamic_signal(const std::vector<double>& section_db, double section_seconds) {
  const std::size_t per_section = static_cast<std::size_t>(section_seconds * kRate);
  Audio audio;
  audio.rate = kSampleRate;
  audio.channels.assign(2, std::vector<float>(per_section * section_db.size()));
  for (std::size_t s = 0; s < section_db.size(); ++s) {
    const double level = std::pow(10.0, section_db[s] / 20.0);
    for (std::size_t k = 0; k < per_section; ++k) {
      const std::size_t n = s * per_section + k;
      const double t = static_cast<double>(n) / kRate;
      const double tone = std::sin(2.0 * kPi * 220.0 * t) + 0.7 * std::sin(2.0 * kPi * 277.2 * t + 1.0) +
                          0.5 * std::sin(2.0 * kPi * 329.6 * t + 2.0);
      const double since_hit = std::fmod(t, 1.0);
      const double hit = 2.5 * std::exp(-since_hit * 40.0) * std::sin(2.0 * kPi * 1500.0 * since_hit);
      audio.channels[0][n] = static_cast<float>(level * 0.25 * (tone + hit));
      audio.channels[1][n] = static_cast<float>(level * 0.25 * (tone - hit * 0.8));
    }
  }
  return audio;
}

struct MeterGuard {
  ebur128_state* state;
  ~MeterGuard() { ebur128_destroy(&state); }
};

// 每秒取一次 3 秒短时响度,丢弃 -70 LUFS 以下的值;返回第 95 与第 10 百分位之差(LU)。
double short_term_spread(const Audio& audio) {
  MeterGuard meter{ebur128_init(2, audio.rate, EBUR128_MODE_S)};
  std::vector<double> values;
  const std::size_t step = audio.rate;
  std::vector<float> interleaved(step * 2);
  for (std::size_t start = 0; start + step <= audio.frames(); start += step) {
    for (std::size_t f = 0; f < step; ++f) {
      interleaved[2 * f] = audio.channels[0][start + f];
      interleaved[2 * f + 1] = audio.channels[1][start + f];
    }
    ebur128_add_frames_float(meter.state, interleaved.data(), step);
    double value = 0.0;
    if (start + step >= 3 * step && ebur128_loudness_shortterm(meter.state, &value) == EBUR128_SUCCESS &&
        std::isfinite(value) && value > -70.0) {
      values.push_back(value);
    }
  }
  if (values.size() < 4) return 0.0;
  std::sort(values.begin(), values.end());
  const auto pick = [&](double p) { return values[static_cast<std::size_t>(p * (values.size() - 1))]; };
  return pick(0.95) - pick(0.10);
}

std::uint64_t fnv(std::uint64_t hash, const void* data, std::size_t bytes) {
  const auto* p = static_cast<const unsigned char*>(data);
  for (std::size_t i = 0; i < bytes; ++i) {
    hash ^= p[i];
    hash *= 1099511628211ULL;
  }
  return hash;
}

std::vector<float> sine(double hz, double amplitude, double phase) {
  std::vector<float> samples(3 * 44100);
  for (std::size_t n = 0; n < samples.size(); ++n) {
    const double t = static_cast<double>(n) / 44100.0;
    samples[n] = static_cast<float>(amplitude * std::sin(2.0 * kPi * hz * t + phase));
  }
  return samples;
}

MixInput pad(const char* family, int channel, double hz, double amplitude) {
  MixInput input;
  input.family = family;
  input.channel = channel;
  input.left = sine(hz, amplitude, 0.0);
  input.right = sine(hz, amplitude, 0.3);
  return input;
}

}  // namespace

// 默认路径(master_dynamics=false)必须与加入动态处理之前逐位一致。
// 期望哈希由加入本级之前的 mixer.cpp/loudness.cpp 构建的程序算出
// (FNV-1a:左声道、右声道的 float 位模式,再接报告文本)。
CGMUSIC_TEST(default_mix_path_is_bit_identical_to_pre_dynamics_baseline) {
  const auto build = [] {
    return std::vector<MixInput>{pad("Strings", 1, 220.0, 0.1), pad("Brass", 2, 330.0, 0.1),
                                 pad("Choir", 3, 440.0, 0.08)};
  };
  const MixResult implicit_default = mix_stems(build(), MixOptions{});
  MixOptions explicit_off;
  explicit_off.master_dynamics = false;
  const MixResult explicit_default = mix_stems(build(), explicit_off);
  CGMUSIC_CHECK(implicit_default.left == explicit_default.left);
  CGMUSIC_CHECK(implicit_default.right == explicit_default.right);
  CGMUSIC_CHECK(implicit_default.report == explicit_default.report);
  CGMUSIC_CHECK(implicit_default.report.find("dynamics") == std::string::npos);

  std::uint64_t hash = 14695981039346656037ULL;
  for (const auto* channel : {&implicit_default.left, &implicit_default.right}) {
    for (const float sample : *channel) {
      std::uint32_t bits = 0;
      std::memcpy(&bits, &sample, sizeof bits);
      hash = fnv(hash, &bits, sizeof bits);
    }
  }
  hash = fnv(hash, implicit_default.report.data(), implicit_default.report.size());
  CGMUSIC_CHECK(hash == 0x73bc1f18df5d23c3ULL);
}

CGMUSIC_TEST(muse_dynamics_reaches_target_with_tp_under_ceiling) {
  Audio audio = dynamic_signal({-40.0, -24.0, -38.0, -30.0}, 6.0);
  DynamicsReport report = compress_master(audio, -14.0, -1.5);
  const Quality quality = measure(audio);
  CGMUSIC_CHECK(std::fabs(quality.lufs - (-14.0)) <= 0.2);
  CGMUSIC_CHECK(quality.true_peak_dbtp <= -1.5);
  CGMUSIC_CHECK(!quality.clipped);
  CGMUSIC_CHECK(std::isfinite(report.makeup_db) && report.makeup_db > 0.0);
  CGMUSIC_CHECK(report.max_gain_reduction_db > 0.0);
  CGMUSIC_CHECK(report.average_gain_reduction_db > 0.0);
  CGMUSIC_CHECK(report.average_gain_reduction_db <= report.max_gain_reduction_db);
  CGMUSIC_CHECK(report.ratio == 2.5);
}

CGMUSIC_TEST(muse_dynamics_narrows_short_term_loudness_spread) {
  const Audio source = dynamic_signal({-40.0, -24.0, -38.0, -30.0}, 6.0);
  Audio plain = source;
  master(plain, -14.0, -1.5);
  Audio compressed = source;
  compress_master(compressed, -14.0, -1.5);
  CGMUSIC_CHECK(short_term_spread(compressed) < short_term_spread(plain));
}

CGMUSIC_TEST(muse_dynamics_never_exceeds_ceiling_sample_or_true_peak) {
  Audio audio = dynamic_signal({-18.0, -12.0, -30.0}, 4.0);
  compress_master(audio, -14.0, -1.5);
  const double ceiling = std::pow(10.0, -1.5 / 20.0);
  for (const auto& channel : audio.channels) {
    for (const float sample : channel) {
      CGMUSIC_CHECK(std::isfinite(sample));
      CGMUSIC_CHECK(std::fabs(sample) <= ceiling);
    }
  }
  CGMUSIC_CHECK(measure(audio).true_peak_dbtp <= -1.5);
}

CGMUSIC_TEST(muse_dynamics_handles_silent_and_very_short_input_without_nan) {
  Audio silent;
  silent.channels.assign(2, std::vector<float>(44100, 0.0F));
  bool silent_rejected = false;
  try {
    compress_master(silent, -14.0, -1.5);
  } catch (const Error& error) {
    silent_rejected = error.code() == "synth_failed";
  }
  CGMUSIC_CHECK(silent_rejected);
  for (const float sample : silent.channels[0]) CGMUSIC_CHECK(sample == 0.0F);

  Audio empty;
  empty.channels.assign(2, {});
  bool empty_rejected = false;
  try {
    compress_master(empty, -14.0, -1.5);
  } catch (const Error& error) {
    empty_rejected = error.code() == "synth_failed";
  }
  CGMUSIC_CHECK(empty_rejected);

  // 远短于一个 400 ms 测量块的输入:响度不可测时报错而非产生 NaN;能测时输出必须有限。
  Audio tiny;
  tiny.channels.assign(2, std::vector<float>(64, 0.0F));
  for (std::size_t n = 0; n < 64; ++n) {
    tiny.channels[0][n] = tiny.channels[1][n] = 0.1F * static_cast<float>(std::sin(0.3 * static_cast<double>(n)));
  }
  try {
    compress_master(tiny, -14.0, -1.5);
  } catch (const Error&) {
  }
  for (const auto& channel : tiny.channels) {
    for (const float sample : channel) CGMUSIC_CHECK(std::isfinite(sample));
  }

  Audio brief = dynamic_signal({-20.0}, 0.6);
  compress_master(brief, -14.0, -1.5);
  for (const auto& channel : brief.channels) {
    for (const float sample : channel) CGMUSIC_CHECK(std::isfinite(sample));
  }
}

CGMUSIC_TEST(muse_mix_enables_dynamics_block_in_report_and_default_does_not) {
  const auto build = [] {
    std::vector<MixInput> inputs{pad("Strings", 1, 220.0, 0.02), pad("Brass", 2, 330.0, 0.02)};
    for (std::size_t n = 0; n < inputs[0].left.size(); ++n) {
      const double swell = n > inputs[0].left.size() / 2 ? 8.0 : 1.0;
      inputs[0].left[n] *= static_cast<float>(swell);
      inputs[0].right[n] *= static_cast<float>(swell);
    }
    return inputs;
  };
  MixOptions options;
  options.master_dynamics = true;
  const MixResult with = mix_stems(build(), options);
  for (const char* key : {"\"dynamics\":{\"threshold_db\":", "\"ratio\":", "\"max_gain_reduction_db\":",
                          "\"avg_gain_reduction_db\":", "\"makeup_db\":"}) {
    CGMUSIC_CHECK(with.report.find(key) != std::string::npos);
  }
  CGMUSIC_CHECK(mix_stems(build(), MixOptions{}).report.find("dynamics") == std::string::npos);
}

}  // namespace cgmusic
