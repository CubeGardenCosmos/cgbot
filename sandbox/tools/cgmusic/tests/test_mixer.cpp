#include <algorithm>
#include <cmath>
#include <cstddef>
#include <optional>
#include <string>
#include <vector>

#include "common.hpp"
#include "loudness.hpp"
#include "mix/mixer.hpp"
#include "mix/reverb.hpp"
#include "test_harness.hpp"

namespace cgmusic {
namespace {

constexpr std::size_t kFrames = 3 * 44100;  // 3 秒合成 pad
constexpr double kPi = 3.14159265358979323846;

std::vector<float> sine(double hz, double amplitude, double phase) {
  std::vector<float> samples(kFrames);
  for (std::size_t n = 0; n < kFrames; ++n) {
    const double t = static_cast<double>(n) / 44100.0;
    samples[n] = static_cast<float>(amplitude * std::sin(2.0 * kPi * hz * t + phase));
  }
  return samples;
}

MixInput pad(const char* family, int channel, double hz, double amplitude,
             std::optional<int> send) {
  MixInput input;
  input.family = family;
  input.channel = channel;
  input.left = sine(hz, amplitude, 0.0);
  input.right = sine(hz, amplitude, 0.3);
  input.reverb_send = send;
  return input;
}

std::vector<MixInput> pad_chord(std::optional<int> send) {
  std::vector<MixInput> inputs;
  inputs.push_back(pad("Strings", 1, 220.0, 0.1, send));
  inputs.push_back(pad("Brass", 2, 330.0, 0.1, send));
  inputs.push_back(pad("Choir", 3, 440.0, 0.08, send));
  return inputs;
}

Audio to_audio(const MixResult& result) {
  Audio audio;
  audio.rate = kSampleRate;
  audio.channels = {result.left, result.right};
  return audio;
}

double max_abs_difference(const MixResult& a, const MixResult& b) {
  double worst = 0.0;
  for (std::size_t n = 0; n < a.left.size(); ++n) {
    worst = std::max(worst, std::fabs(static_cast<double>(a.left[n]) - b.left[n]));
    worst = std::max(worst, std::fabs(static_cast<double>(a.right[n]) - b.right[n]));
  }
  return worst;
}

CGMUSIC_TEST(mixer_is_deterministic) {
  const MixResult first = mix_stems(pad_chord(std::nullopt), MixOptions{});
  const MixResult second = mix_stems(pad_chord(std::nullopt), MixOptions{});
  CGMUSIC_CHECK(first.left == second.left);
  CGMUSIC_CHECK(first.right == second.right);
  CGMUSIC_CHECK(first.report == second.report);
}

CGMUSIC_TEST(send_zero_forces_dry_and_differs_from_default) {
  const MixResult defaults = mix_stems(pad_chord(std::nullopt), MixOptions{});
  const MixResult forced_dry = mix_stems(pad_chord(0), MixOptions{});
  CGMUSIC_CHECK(max_abs_difference(defaults, forced_dry) > 1e-4);

  // send 0 的输出必须与关闭总线混响的输出逐采样相同(湿声精确为零)。
  MixOptions no_reverb;
  no_reverb.reverb_level = 0.0;
  const MixResult no_bus = mix_stems(pad_chord(std::nullopt), no_reverb);
  CGMUSIC_CHECK(max_abs_difference(forced_dry, no_bus) == 0.0);
}

CGMUSIC_TEST(explicit_nonzero_send_beats_family_default) {
  const MixResult defaults = mix_stems(pad_chord(std::nullopt), MixOptions{});
  const MixResult explicit_send = mix_stems(pad_chord(127), MixOptions{});
  CGMUSIC_CHECK(max_abs_difference(defaults, explicit_send) > 1e-4);
  CGMUSIC_CHECK(explicit_send.report.find("\"send_source\":\"explicit\"") != std::string::npos);
  CGMUSIC_CHECK(defaults.report.find("\"send_source\":\"default\"") != std::string::npos);
}

CGMUSIC_TEST(family_tables_and_stem_mapping) {
  CGMUSIC_CHECK(default_reverb_send("Choir") == 55);
  CGMUSIC_CHECK(default_reverb_send("unknown-family") == 30);
  CGMUSIC_CHECK(stem_for_family("Woodwinds") == Stem::Woodwind);
  CGMUSIC_CHECK(stem_for_family("BRASS") == Stem::Brass);
  CGMUSIC_CHECK(stem_for_family("Strings") == Stem::Melodic);
}

CGMUSIC_TEST(aux_reverb_energy_is_bounded_and_decays) {
  AuxReverb reverb(44100.0, kVideoMakerRoomSize, kVideoMakerDamping, kVideoMakerWidth);
  const std::size_t total = 4 * 44100;
  double energy = 0.0;
  double first_second = 0.0;
  double last_second = 0.0;
  for (std::size_t n = 0; n < total; ++n) {
    const float x = n == 0 ? 1.0F : 0.0F;
    float left = 0.0F;
    float right = 0.0F;
    reverb.process(x, x, left, right);
    const double e = static_cast<double>(left) * left + static_cast<double>(right) * right;
    energy += e;
    if (n < 44100) first_second += e;
    if (n >= total - 44100) last_second += e;
  }
  CGMUSIC_CHECK(std::isfinite(energy));
  CGMUSIC_CHECK(energy > 0.0);
  // 被动性:冲激输入能量为 1^2 + 1^2 = 2,输出总能量须不超过输入(实测约 0.69,余量充足)。
  CGMUSIC_CHECK(energy <= 2.0);
  CGMUSIC_CHECK(last_second < first_second);
}

CGMUSIC_TEST(loudness_hits_target_on_synthetic_pad) {
  const MixResult result = mix_stems(pad_chord(std::nullopt), MixOptions{});
  const Quality quality = measure(to_audio(result));
  CGMUSIC_CHECK(std::fabs(quality.lufs - (-14.0)) <= 0.1);
  CGMUSIC_CHECK(quality.true_peak_dbtp <= -1.5);
}

CGMUSIC_TEST(clipping_never_happens_on_full_scale_input) {
  std::vector<MixInput> loud;
  loud.push_back(pad("Strings", 1, 220.0, 1.0, 127));
  loud.push_back(pad("Brass", 2, 330.0, 1.0, 127));
  loud.push_back(pad("Choir", 3, 440.0, 1.0, 127));
  loud.push_back(pad("Percussion", 10, 110.0, 1.0, 127));
  const MixResult result = mix_stems(loud, MixOptions{});
  const Quality quality = measure(to_audio(result));
  CGMUSIC_CHECK(!quality.clipped);
  CGMUSIC_CHECK(quality.true_peak_dbtp <= -1.5);
  for (std::size_t n = 0; n < result.left.size(); ++n) {
    CGMUSIC_CHECK(std::fabs(result.left[n]) < 1.0F);
    CGMUSIC_CHECK(std::fabs(result.right[n]) < 1.0F);
  }
}

}  // namespace
}  // namespace cgmusic
