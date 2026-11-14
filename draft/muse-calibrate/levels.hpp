#pragma once

#include <string>
#include <string_view>
#include <vector>

namespace cgmusic::muse {

struct LevelCalibration {
  std::string name;              // 乐器名称或匹配键 (如 "Piano", "Violins 1", "Drum Kit")
  std::string family;            // 乐器家族 ("woodwinds", "brass", "strings", "keys", "percussion", etc.)
  double measured_mf = 0.0;      // 实测 mf (力度 80) 发音窗口 RMS (dBFS)
  double target_mf = 0.0;        // 物理/声学原则目标相对电平 (dBFS)
  double gain_db = 0.0;          // target_mf - measured_mf (在 mix_hybrid 中作为固定增益施加)
  double slope = 0.20;           // 力度电平斜率: (L(100) - L(50)) / 50.0 (dB/力度单位)
  double uncertainty_db = 1.0;   // 声学不确定度 (+/- dB)
  std::string source;            // 声学文献出处与依据
};

// 查找指定乐器名称、家族和 GM 编号的标定记录。保证不返回空引用,未精确匹配时回退到家族或全局默认。
const LevelCalibration& find_calibration(std::string_view instrument_name,
                                         std::string_view family,
                                         int program = -1);

// 获取已提交的标定表全部条目
const std::vector<LevelCalibration>& calibration_table();

}  // namespace cgmusic::muse
