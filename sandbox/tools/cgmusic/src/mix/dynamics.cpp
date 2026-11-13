#include "mix/dynamics.hpp"

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <deque>
#include <limits>
#include <utility>
#include <vector>

#include "loudness.hpp"

namespace cgmusic {
namespace {

double db_to_linear(double db) { return std::pow(10.0, db / 20.0); }

double linear_to_db(double value) {
  return value > 0.0 ? 20.0 * std::log10(value)
                     : -std::numeric_limits<double>::infinity();
}

void apply_gain(Audio& audio, double gain_db) {
  const auto gain = static_cast<float>(db_to_linear(gain_db));
  for (auto& channel : audio.channels) {
    for (float& sample : channel) sample *= gain;
  }
}

// 双二阶高通,仅用于侧链。RBJ cookbook,Q=1/sqrt(2) 的 Butterworth 响应。
class SidechainHighPass {
 public:
  SidechainHighPass(double rate, double cutoff) {
    constexpr double kPi = 3.14159265358979323846;
    constexpr double kQ = 0.70710678118654752440;
    const double w0 = 2.0 * kPi * cutoff / rate;
    const double cosine = std::cos(w0);
    const double alpha = std::sin(w0) / (2.0 * kQ);
    const double a0 = 1.0 + alpha;
    b0_ = (1.0 + cosine) / (2.0 * a0);
    b1_ = -(1.0 + cosine) / a0;
    b2_ = b0_;
    a1_ = -2.0 * cosine / a0;
    a2_ = (1.0 - alpha) / a0;
  }

  double process(double input) {
    const double output = b0_ * input + b1_ * x1_ + b2_ * x2_ - a1_ * y1_ - a2_ * y2_;
    x2_ = x1_;
    x1_ = input;
    y2_ = y1_;
    y1_ = output;
    return output;
  }

 private:
  double b0_ = 0.0;
  double b1_ = 0.0;
  double b2_ = 0.0;
  double a1_ = 0.0;
  double a2_ = 0.0;
  double x1_ = 0.0;
  double x2_ = 0.0;
  double y1_ = 0.0;
  double y2_ = 0.0;
};

// 先求每一帧需要的前瞻最小增益,再用同一半窗的均值平滑。
// 峰值 i 位于任一覆盖样本 j 的窗口内,因此平滑后的 gain[j] <= need[i],不会越过该峰值。
void lookahead_limit(Audio& audio, double ceiling, double half_window_seconds) {
  const std::size_t n = audio.frames();
  if (n == 0) return;
  const auto half = std::max<std::size_t>(
      1, static_cast<std::size_t>(audio.rate * half_window_seconds));
  std::vector<float> gain(n);
  for (std::size_t i = 0; i < n; ++i) {
    const float peak = std::max(std::fabs(audio.channels[0][i]), std::fabs(audio.channels[1][i]));
    gain[i] = peak > ceiling ? static_cast<float>(ceiling / peak) : 1.0F;
  }

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

}  // namespace

DynamicsReport compress_master(Audio& audio, double target_lufs, double ceiling_dbtp,
                               const DynamicsSettings& settings, Quality* final_quality) {
  if (audio.channels.size() != 2 || audio.channels[0].size() != audio.channels[1].size()) {
    throw Error("bad_input", "Master dynamics expects stereo audio");
  }
  if (!(settings.ratio >= 1.0) || !(settings.knee_db > 0.0) ||
      !(settings.attack_ms > 0.0) || !(settings.fast_release_ms > 0.0) ||
      !(settings.slow_release_ms >= settings.fast_release_ms) ||
      !(settings.sidechain_hp_hz > 0.0) ||
      !(settings.sidechain_hp_hz < audio.rate / 2.0) || !(settings.lookahead_ms > 0.0)) {
    throw Error("bad_input", "Invalid master dynamics settings");
  }

  const std::size_t frames = audio.frames();
  const Quality input = measure(audio);
  DynamicsReport report;
  report.ratio = settings.ratio;
  report.threshold_db = std::isfinite(input.lufs)
                            ? input.lufs + settings.threshold_offset_db
                            : -std::numeric_limits<double>::infinity();
  if (!std::isfinite(input.lufs)) {
    master(audio, target_lufs, ceiling_dbtp);  // 全静音/空输入:抛出 synth_failed。
    throw Error("synth_failed", "The score rendered silence");
  }

  SidechainHighPass hp_left(audio.rate, settings.sidechain_hp_hz);
  SidechainHighPass hp_right(audio.rate, settings.sidechain_hp_hz);
  const double attack = std::exp(-1.0 / (audio.rate * settings.attack_ms / 1000.0));
  const double fast_release = std::exp(-1.0 / (audio.rate * settings.fast_release_ms / 1000.0));
  const double slow_release = std::exp(-1.0 / (audio.rate * settings.slow_release_ms / 1000.0));
  const double half_knee = settings.knee_db / 2.0;
  const double slope = 1.0 - 1.0 / settings.ratio;
  // 两级释放:同一攻击,快/慢两路各自恢复,增益衰减取两路均值。
  // 瞬态后先快速回升一半,其余缓慢恢复,既不拖尾也不抽吸。
  double fast_reduction = 0.0;
  double slow_reduction = 0.0;
  double reduction_sum = 0.0;

  for (std::size_t i = 0; i < frames; ++i) {
    const double left = audio.channels[0][i];
    const double right = audio.channels[1][i];
    const double sidechain = std::max(std::fabs(hp_left.process(left)),
                                      std::fabs(hp_right.process(right)));
    const double level_db = linear_to_db(sidechain);
    double over = level_db - report.threshold_db;
    if (over <= -half_knee) {
      over = 0.0;
    } else if (over < half_knee) {
      over = (over + half_knee) * (over + half_knee) / (2.0 * settings.knee_db);
    }
    const double target_reduction = slope * over;
    fast_reduction = target_reduction > fast_reduction
                         ? attack * fast_reduction + (1.0 - attack) * target_reduction
                         : fast_release * fast_reduction + (1.0 - fast_release) * target_reduction;
    slow_reduction = target_reduction > slow_reduction
                         ? attack * slow_reduction + (1.0 - attack) * target_reduction
                         : slow_release * slow_reduction + (1.0 - slow_release) * target_reduction;
    const double reduction = 0.5 * (fast_reduction + slow_reduction);
    const double gain = db_to_linear(-reduction);
    audio.channels[0][i] = static_cast<float>(left * gain);
    audio.channels[1][i] = static_cast<float>(right * gain);
    reduction_sum += reduction;
    report.max_gain_reduction_db = std::max(report.max_gain_reduction_db, reduction);
  }
  report.average_gain_reduction_db = reduction_sum / static_cast<double>(frames);

  const Quality compressed = measure(audio);
  if (!std::isfinite(compressed.lufs)) throw Error("synth_failed", "The score rendered silence");
  // 补足到目标响度。压缩降低了峰值/响度比,这里让慢段落也获得增益,而非仅补偿固定 dB。
  report.makeup_db = target_lufs - compressed.lufs;
  apply_gain(audio, report.makeup_db);
  // 用略低于最终上限的前瞻砖墙峰值级处理明显瞬态,TP 迭代只做小幅过采样修正。
  const double pre_ceiling = db_to_linear(ceiling_dbtp - settings.limiter_margin_db);
  lookahead_limit(audio, pre_ceiling, settings.lookahead_ms / 2000.0);
  const Quality output = master(audio, target_lufs, ceiling_dbtp);
  if (final_quality) *final_quality = output;
  return report;
}

}  // namespace cgmusic
