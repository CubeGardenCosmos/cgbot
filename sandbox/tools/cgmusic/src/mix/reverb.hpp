#pragma once

#include <array>
#include <cstddef>
#include <vector>

namespace cgmusic {

// 辅助总线混响,结构移植自 VideoMaker voice/reverb_runner.py::render_reverb 的 Pedalboard 预设:
//   HighpassFilter(150 Hz) -> Reverb(room_size 0.75, damping 0.4, wet_level 1.0, dry_level 0.0,
//   width 1.0) -> LowpassFilter(8500 Hz),只输出湿声。
// 注意:Pedalboard 的 Reverb 为 JUCE 实现;此处以 Freeverb 结构近似,不是逐采样一致。
// VideoMaker 预设没有预延迟参数,此处同样没有。全部缓冲在构造时分配,处理循环内无分配。
inline constexpr double kVideoMakerRoomSize = 0.75;
inline constexpr double kVideoMakerDamping = 0.4;
inline constexpr double kVideoMakerWidth = 1.0;
inline constexpr double kVideoMakerHighpassHz = 150.0;
inline constexpr double kVideoMakerLowpassHz = 8500.0;

// RBJ 二阶滤波器(Q = 1/sqrt(2)),双精度状态,直接 II 转置型。
class Biquad {
 public:
  enum class Kind { HighPass, LowPass };

  Biquad(Kind kind, double rate, double cutoff_hz);
  float process(float input) noexcept;

 private:
  double b0_;
  double b1_;
  double b2_;
  double a1_;
  double a2_;
  double z1_ = 0.0;
  double z2_ = 0.0;
};

// Freeverb 核心:每声道 8 路并联梳状滤波器 + 4 级串联全通;右声道延迟扩展 23 个采样(44.1 kHz 基准)。
// room 0-1 映射为梳状反馈 room*0.28+0.7;damping 0-1 映射为梳状阻尼 damping*0.4;width 0-1 为立体声宽度。
// 输出为湿声,增益固定(不随输入电平归一化)。
class FreeVerb {
 public:
  FreeVerb(double rate, double room, double damping, double width);
  void process(float in_l, float in_r, float& out_l, float& out_r) noexcept;

 private:
  struct Comb {
    std::vector<float> buffer;
    std::size_t index = 0;
    float store = 0.0F;
  };
  struct Allpass {
    std::vector<float> buffer;
    std::size_t index = 0;
  };

  std::array<Comb, 8> combs_l_;
  std::array<Comb, 8> combs_r_;
  std::array<Allpass, 4> allpass_l_;
  std::array<Allpass, 4> allpass_r_;
  float feedback_ = 0.0F;
  float damp1_ = 0.0F;
  float damp2_ = 0.0F;
  float wet1_ = 0.0F;
  float wet2_ = 0.0F;
};

// 总线混响:HP 150 Hz -> FreeVerb -> LP 8.5 kHz,输出湿声(未乘总线电平)。
class AuxReverb {
 public:
  AuxReverb(double rate, double room, double damping, double width);
  void process(float in_l, float in_r, float& out_l, float& out_r) noexcept;

 private:
  Biquad hp_l_;
  Biquad hp_r_;
  Biquad lp_l_;
  Biquad lp_r_;
  FreeVerb verb_;
};

}  // namespace cgmusic
