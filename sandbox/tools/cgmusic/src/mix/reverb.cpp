#include "mix/reverb.hpp"

#include <cmath>
#include <cstddef>

namespace cgmusic {
namespace {

constexpr double kPi = 3.14159265358979323846;
constexpr double kReferenceRate = 44100.0;

// Freeverb 标准调谐(44.1 kHz)与参数映射常数。
constexpr std::array<std::size_t, 8> kCombTuning{1116, 1188, 1277, 1356, 1422, 1491, 1557, 1617};
constexpr std::array<std::size_t, 4> kAllpassTuning{556, 441, 341, 225};
constexpr std::size_t kStereoSpread = 23;
constexpr float kFixedGain = 0.015F;
constexpr float kAllpassFeedback = 0.5F;
constexpr double kScaleRoom = 0.28;
constexpr double kOffsetRoom = 0.7;
constexpr double kScaleDamp = 0.4;

std::size_t scaled_length(std::size_t samples, double rate) {
  const auto length = static_cast<std::size_t>(
      std::lround(static_cast<double>(samples) * rate / kReferenceRate));
  return length == 0 ? 1 : length;
}

}  // namespace

Biquad::Biquad(Kind kind, double rate, double cutoff_hz) {
  // RBJ cookbook,Q = 1/sqrt(2)(Butterworth 形状)。
  const double w0 = 2.0 * kPi * cutoff_hz / rate;
  const double cos_w0 = std::cos(w0);
  const double alpha = std::sin(w0) / std::sqrt(2.0);
  double b0 = 0.0;
  double b1 = 0.0;
  double b2 = 0.0;
  if (kind == Kind::HighPass) {
    b0 = (1.0 + cos_w0) / 2.0;
    b1 = -(1.0 + cos_w0);
    b2 = (1.0 + cos_w0) / 2.0;
  } else {
    b0 = (1.0 - cos_w0) / 2.0;
    b1 = 1.0 - cos_w0;
    b2 = (1.0 - cos_w0) / 2.0;
  }
  const double a0 = 1.0 + alpha;
  b0_ = b0 / a0;
  b1_ = b1 / a0;
  b2_ = b2 / a0;
  a1_ = (-2.0 * cos_w0) / a0;
  a2_ = (1.0 - alpha) / a0;
}

float Biquad::process(float input) noexcept {
  const double x = static_cast<double>(input);
  const double y = b0_ * x + z1_;
  z1_ = b1_ * x - a1_ * y + z2_;
  z2_ = b2_ * x - a2_ * y;
  return static_cast<float>(y);
}

FreeVerb::FreeVerb(double rate, double room, double damping, double width)
    : feedback_(static_cast<float>(room * kScaleRoom + kOffsetRoom)),
      damp1_(static_cast<float>(damping * kScaleDamp)),
      damp2_(1.0F - damp1_),
      wet1_(static_cast<float>(width / 2.0 + 0.5)),
      wet2_(static_cast<float>((1.0 - width) / 2.0)) {
  for (std::size_t i = 0; i < kCombTuning.size(); ++i) {
    combs_l_[i].buffer.assign(scaled_length(kCombTuning[i], rate), 0.0F);
    combs_r_[i].buffer.assign(scaled_length(kCombTuning[i] + kStereoSpread, rate), 0.0F);
  }
  for (std::size_t i = 0; i < kAllpassTuning.size(); ++i) {
    allpass_l_[i].buffer.assign(scaled_length(kAllpassTuning[i], rate), 0.0F);
    allpass_r_[i].buffer.assign(scaled_length(kAllpassTuning[i] + kStereoSpread, rate), 0.0F);
  }
}

void FreeVerb::process(float in_l, float in_r, float& out_l, float& out_r) noexcept {
  // Freeverb 以单声道和输入驱动两组梳状滤波器,右组带相位扩展。
  const float input = (in_l + in_r) * kFixedGain;

  const auto comb = [this, input](Comb& c) noexcept -> float {
    const float output = c.buffer[c.index];
    c.store = output * damp2_ + c.store * damp1_;
    c.buffer[c.index] = input + c.store * feedback_;
    if (++c.index == c.buffer.size()) c.index = 0;
    return output;
  };
  const auto allpass = [](Allpass& a, float x) noexcept -> float {
    const float bufout = a.buffer[a.index];
    const float output = -x + bufout;
    a.buffer[a.index] = x + bufout * kAllpassFeedback;
    if (++a.index == a.buffer.size()) a.index = 0;
    return output;
  };

  float l = 0.0F;
  float r = 0.0F;
  for (auto& c : combs_l_) l += comb(c);
  for (auto& c : combs_r_) r += comb(c);
  for (auto& a : allpass_l_) l = allpass(a, l);
  for (auto& a : allpass_r_) r = allpass(a, r);

  out_l = l * wet1_ + r * wet2_;
  out_r = r * wet1_ + l * wet2_;
}

AuxReverb::AuxReverb(double rate, double room, double damping, double width)
    : hp_l_(Biquad::Kind::HighPass, rate, kVideoMakerHighpassHz),
      hp_r_(Biquad::Kind::HighPass, rate, kVideoMakerHighpassHz),
      lp_l_(Biquad::Kind::LowPass, rate, kVideoMakerLowpassHz),
      lp_r_(Biquad::Kind::LowPass, rate, kVideoMakerLowpassHz),
      verb_(rate, room, damping, width) {}

void AuxReverb::process(float in_l, float in_r, float& out_l, float& out_r) noexcept {
  float wet_l = 0.0F;
  float wet_r = 0.0F;
  verb_.process(hp_l_.process(in_l), hp_r_.process(in_r), wet_l, wet_r);
  out_l = lp_l_.process(wet_l);
  out_r = lp_r_.process(wet_r);
}

}  // namespace cgmusic
