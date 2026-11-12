#include <cmath>
#include <cstddef>
#include <functional>
#include <string>
#include <vector>

#include "common.hpp"
#include "loudness.hpp"
#include "test_harness.hpp"

namespace {

constexpr double kPi = 3.14159265358979323846;

cgmusic::Audio sine(double frequency, double amplitude, double seconds) {
  cgmusic::Audio audio;
  audio.rate = cgmusic::kSampleRate;
  const auto n = static_cast<std::size_t>(seconds * cgmusic::kSampleRate);
  std::vector<float> channel(n);
  for (std::size_t i = 0; i < n; ++i) {
    channel[i] = static_cast<float>(amplitude * std::sin(2.0 * kPi * frequency * static_cast<double>(i) /
                                                         cgmusic::kSampleRate));
  }
  audio.channels = {channel, channel};
  return audio;
}

bool near(double a, double b, double tolerance) { return std::fabs(a - b) <= tolerance; }

std::string error_code_of(const std::function<void()>& action) {
  try {
    action();
  } catch (const cgmusic::Error& error) {
    return error.code();
  }
  return "none";
}

}  // namespace

// 0.05 峰值的 1 kHz 正弦约 -26.7 LUFS,增益约 +12.7 dB 后真峰值约 -13 dBTP,无需限制器。
CGMUSIC_TEST(quiet_sine_reaches_minus_14_lufs_without_limiting) {
  cgmusic::Audio audio = sine(1000.0, 0.05, 10.0);
  const cgmusic::Quality quality = cgmusic::master(audio, -14.0, -1.5);
  CGMUSIC_CHECK(near(quality.lufs, -14.0, 0.2));
  CGMUSIC_CHECK(quality.true_peak_dbtp <= -1.5);
  CGMUSIC_CHECK(near(quality.duration_s, 10.0, 1e-6));
  CGMUSIC_CHECK(!quality.clipped);
}

// 稀疏满幅脉冲的响度远低于峰值,增益会把脉冲推过上限,必须由限制器压住。
CGMUSIC_TEST(limiter_holds_true_peak_under_ceiling) {
  cgmusic::Audio audio = sine(1000.0, 0.001, 10.0);
  for (std::size_t i = 11025; i < audio.frames(); i += 22050) {
    audio.channels[0][i] = 0.95F;
    audio.channels[1][i] = 0.95F;
  }
  const cgmusic::Quality before = cgmusic::measure(audio);
  CGMUSIC_CHECK(before.true_peak_dbtp > -1.5);  // 确认该信号确实需要限制

  const cgmusic::Quality after = cgmusic::master(audio, -14.0, -1.5);
  CGMUSIC_CHECK(after.true_peak_dbtp <= -1.5);
  CGMUSIC_CHECK(std::isfinite(after.lufs));
  for (const auto& channel : audio.channels) {
    for (const float sample : channel) CGMUSIC_CHECK(std::fabs(sample) < 0.9F);
  }
}

// 前后各 1 秒静音,中间 1 秒正弦:裁剪后约 1 秒;末尾淡出最后一帧为 0。
CGMUSIC_TEST(trim_silence_and_fade_tail) {
  cgmusic::Audio audio;
  audio.rate = cgmusic::kSampleRate;
  const std::size_t second = cgmusic::kSampleRate;
  const cgmusic::Audio tone = sine(1000.0, 0.5, 1.0);
  std::vector<float> channel(second, 0.0F);
  channel.insert(channel.end(), tone.channels[0].begin(), tone.channels[0].end());
  channel.insert(channel.end(), second, 0.0F);
  audio.channels = {channel, channel};

  cgmusic::trim_silence(audio);
  CGMUSIC_CHECK(audio.frames() >= second - 100 && audio.frames() <= second);

  cgmusic::fade_tail(audio, 0.5);
  CGMUSIC_CHECK(audio.channels[0].back() == 0.0F);
  CGMUSIC_CHECK(std::fabs(audio.channels[0][audio.frames() - 1 - second / 2]) < 0.5F);
}

CGMUSIC_TEST(all_silence_is_rejected_as_synth_failed) {
  cgmusic::Audio audio;
  audio.rate = cgmusic::kSampleRate;
  audio.channels = {std::vector<float>(1000, 0.0F), std::vector<float>(1000, 0.0F)};
  CGMUSIC_CHECK(error_code_of([&] { cgmusic::trim_silence(audio); }) == "synth_failed");
}

// 线性增益会让脉冲超过真峰值上限,需要限制器;压限后响度应收敛到目标 ±0.2 LU,真峰值仍在上限内。
CGMUSIC_TEST(limited_master_converges_to_target_loudness) {
  cgmusic::Audio audio = sine(1000.0, 0.05, 10.0);
  for (std::size_t i = 0; i < audio.frames(); i += 4410) {
    audio.channels[0][i] = 0.9F;
    audio.channels[1][i] = 0.9F;
  }
  const cgmusic::Quality quality = cgmusic::master(audio, -14.0, -1.5);
  CGMUSIC_CHECK(near(quality.lufs, -14.0, 0.2));
  CGMUSIC_CHECK(quality.true_peak_dbtp <= -1.5);
}
