#include "loudness.hpp"

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <deque>
#include <limits>
#include <memory>
#include <optional>
#include <utility>
#include <vector>

#include <ebur128.h>

namespace cgmusic {
namespace {

constexpr float kSilenceThreshold = 0.001F;  // -60 dBFS
constexpr int kMaxLimiterAttempts = 8;
constexpr std::size_t kChunkFrames = 4096;
constexpr double kLimiterHalfWindowSeconds = 0.005;  // 5 ms 前瞻/保持
constexpr double kOvershootMarginDb = 0.05;
constexpr int kMasterIterations = 6;
constexpr double kLoudnessToleranceLu = 0.1;
constexpr double kMaxStepDb = 6.0;
constexpr double kMinLoudnessSlope = 0.1;
constexpr double kMaxLoudnessSlope = 1.5;

struct MeterDeleter {
  void operator()(ebur128_state* state) const { ebur128_destroy(&state); }
};
using MeterPtr = std::unique_ptr<ebur128_state, MeterDeleter>;

double db_to_linear(double db) { return std::pow(10.0, db / 20.0); }

void apply_gain(Audio& audio, float gain) {
  for (auto& channel : audio.channels) {
    for (auto& sample : channel) sample *= gain;
  }
}

// 声道联动的前瞻限制器。
// 1) 每帧所需增益 need[i] = min(1, threshold / 峰值);
// 2) 窗口 [i-h, i+h] 内取最小值(峰值前即降增益);
// 3) 同窗口取均值平滑。对峰值 i,任一 j 的窗口都包含 i,故每帧增益 ≤ need[i]。
// 三步都在同一个数组里原地完成,避免额外的全长缓冲。
void limit_peaks(Audio& audio, float threshold) {
  const std::size_t n = audio.frames();
  if (n == 0) return;
  const auto half = std::max<std::size_t>(
      1, static_cast<std::size_t>(static_cast<double>(audio.rate) * kLimiterHalfWindowSeconds));

  std::vector<float> gain(n);
  for (std::size_t i = 0; i < n; ++i) {
    float peak = 0.0F;
    for (const auto& channel : audio.channels) peak = std::max(peak, std::fabs(channel[i]));
    gain[i] = peak > threshold ? threshold / peak : 1.0F;
  }

  // 滑动最小值:队列存 (索引, 原值)。第 i 帧写入前,next 之后的元素尚未被覆盖。
  std::deque<std::pair<std::size_t, float>> minima;
  std::size_t next = 0;
  for (std::size_t i = 0; i < n; ++i) {
    const std::size_t right = std::min(n - 1, i + half);
    while (next <= right) {
      while (!minima.empty() && minima.back().second >= gain[next]) minima.pop_back();
      minima.emplace_back(next, gain[next]);
      ++next;
    }
    const std::size_t left = i > half ? i - half : 0;
    while (minima.front().first < left) minima.pop_front();
    gain[i] = minima.front().second;
  }

  // 滑动均值:窗口内的值存入 window,离开窗口时按原值减去。
  std::deque<float> window;
  double sum = 0.0;
  std::size_t added = 0;
  std::size_t dropped = 0;
  for (std::size_t i = 0; i < n; ++i) {
    const std::size_t right = std::min(n, i + half + 1);
    while (added < right) {
      window.push_back(gain[added]);
      sum += gain[added];
      ++added;
    }
    const std::size_t left = i > half ? i - half : 0;
    while (dropped < left) {
      sum -= window.front();
      window.pop_front();
      ++dropped;
    }
    gain[i] = static_cast<float>(sum / static_cast<double>(window.size()));
  }

  for (auto& channel : audio.channels) {
    for (std::size_t i = 0; i < n; ++i) channel[i] *= gain[i];
  }
}

// 阈值迭代:以限制后的真峰值超标量收紧阈值,直到真峰值不超过 ceiling_dbtp。
Quality limit_to_ceiling(Audio& audio, double ceiling_dbtp) {
  double threshold = db_to_linear(ceiling_dbtp);
  Quality current = measure(audio);
  for (int attempt = 0; attempt < kMaxLimiterAttempts; ++attempt) {
    if (current.true_peak_dbtp <= ceiling_dbtp) return current;
    limit_peaks(audio, static_cast<float>(threshold));
    // 以限制后的真峰值超标量收紧阈值(过采样过冲);不使用限制前的超标量,避免重复压缩。
    current = measure(audio);
    threshold *= db_to_linear(-std::max(0.0, current.true_peak_dbtp - ceiling_dbtp) - kOvershootMarginDb);
  }
  if (current.true_peak_dbtp > ceiling_dbtp) {
    throw Error("synth_failed", "Could not bring the true peak under the ceiling");
  }
  return current;
}

}  // namespace

Quality measure(const Audio& audio) {
  const std::size_t channels = audio.channels.size();
  if (channels == 0 || channels > 5) throw Error("unsupported", "Unsupported channel count");

  MeterPtr meter(ebur128_init(static_cast<unsigned>(channels), audio.rate,
                              EBUR128_MODE_I | EBUR128_MODE_LRA | EBUR128_MODE_TRUE_PEAK));
  if (!meter) throw Error("unsupported", "Loudness meter rejected the sample rate");

  const std::size_t frames = audio.frames();
  std::vector<float> interleaved(kChunkFrames * channels);
  float sample_peak = 0.0F;
  for (std::size_t start = 0; start < frames; start += kChunkFrames) {
    const std::size_t count = std::min(kChunkFrames, frames - start);
    for (std::size_t f = 0; f < count; ++f) {
      for (std::size_t c = 0; c < channels; ++c) {
        const float value = audio.channels[c][start + f];
        interleaved[f * channels + c] = value;
        sample_peak = std::max(sample_peak, std::fabs(value));
      }
    }
    if (ebur128_add_frames_float(meter.get(), interleaved.data(), count) != EBUR128_SUCCESS) {
      throw Error("synth_failed", "Loudness measurement failed");
    }
  }

  double lufs = 0.0;
  double lra = 0.0;
  if (ebur128_loudness_global(meter.get(), &lufs) != EBUR128_SUCCESS ||
      ebur128_loudness_range(meter.get(), &lra) != EBUR128_SUCCESS) {
    throw Error("synth_failed", "Loudness measurement failed");
  }
  double true_peak = 0.0;
  for (std::size_t c = 0; c < channels; ++c) {
    double peak = 0.0;
    if (ebur128_true_peak(meter.get(), static_cast<unsigned>(c), &peak) != EBUR128_SUCCESS) {
      throw Error("synth_failed", "True peak measurement failed");
    }
    true_peak = std::max(true_peak, peak);
  }

  Quality quality;
  quality.lufs = lufs;
  quality.lra = lra;
  quality.true_peak_dbtp = true_peak > 0.0 ? 20.0 * std::log10(true_peak)
                                           : -std::numeric_limits<double>::infinity();
  quality.clipped = sample_peak >= 1.0F;
  quality.duration_s = static_cast<double>(frames) / static_cast<double>(audio.rate);
  return quality;
}

void trim_silence(Audio& audio) {
  const std::size_t n = audio.frames();
  bool found = false;
  std::size_t first = 0;
  std::size_t last = 0;
  for (std::size_t i = 0; i < n; ++i) {
    for (const auto& channel : audio.channels) {
      if (std::fabs(channel[i]) > kSilenceThreshold) {
        if (!found) {
          first = i;
          found = true;
        }
        last = i;
        break;
      }
    }
  }
  if (!found) throw Error("synth_failed", "The score rendered silence");
  for (auto& channel : audio.channels) {
    channel.erase(channel.begin() + static_cast<std::ptrdiff_t>(last) + 1, channel.end());
    channel.erase(channel.begin(), channel.begin() + static_cast<std::ptrdiff_t>(first));
  }
}

void fade_tail(Audio& audio, double seconds) {
  const std::size_t n = audio.frames();
  const auto length = std::min(n, static_cast<std::size_t>(seconds * audio.rate));
  if (length == 0) return;
  for (auto& channel : audio.channels) {
    for (std::size_t k = 0; k < length; ++k) {
      const double gain = static_cast<double>(length - k - 1) / static_cast<double>(length);
      channel[n - length + k] *= static_cast<float>(gain);
    }
  }
}

// 线性增益对准目标响度;若真峰值会超过上限,限制器会压掉部分响度,
// 此时在增益偏移上用割线法迭代:每轮都从原始样本重新推导(限制器非线性,不可逆),
// 直到限制后的响度落在目标 ±kLoudnessToleranceLu 内。
Quality master(Audio& audio, double target_lufs, double ceiling_dbtp) {
  const Quality measured = measure(audio);
  if (!std::isfinite(measured.lufs)) throw Error("synth_failed", "The score rendered silence");
  const double linear_db = target_lufs - measured.lufs;
  // 线性增益时真峰值与增益同步变化,无需试增益即可判定是否需要限制器。
  if (measured.true_peak_dbtp + linear_db <= ceiling_dbtp) {
    apply_gain(audio, static_cast<float>(db_to_linear(linear_db)));
    return measure(audio);
  }

  const auto original = audio.channels;
  const auto run = [&](double offset_db) {
    audio.channels = original;
    apply_gain(audio, static_cast<float>(db_to_linear(offset_db)));
    return limit_to_ceiling(audio, ceiling_dbtp);
  };

  double offset_db = linear_db;
  double best_offset_db = offset_db;
  double best_error = std::numeric_limits<double>::infinity();
  std::optional<std::pair<double, double>> previous;  // (增益偏移, 限制后响度)
  for (int iteration = 0; iteration < kMasterIterations; ++iteration) {
    const Quality current = run(offset_db);
    const double error = current.lufs - target_lufs;
    if (std::fabs(error) < best_error) {
      best_error = std::fabs(error);
      best_offset_db = offset_db;
    }
    if (std::fabs(error) <= kLoudnessToleranceLu) return current;
    double slope = 1.0;  // 首轮假定 1 dB/dB,之后用割线估计
    if (previous && std::fabs(offset_db - previous->first) > 1e-6) {
      slope = (current.lufs - previous->second) / (offset_db - previous->first);
    }
    slope = std::clamp(slope, kMinLoudnessSlope, kMaxLoudnessSlope);
    previous = std::make_pair(offset_db, current.lufs);
    offset_db += std::clamp(-error / slope, -kMaxStepDb, kMaxStepDb);
  }
  // 未收敛(响度无法在真峰值上限内达到目标):取误差最小的增益。
  return run(best_offset_db);
}

}  // namespace cgmusic
