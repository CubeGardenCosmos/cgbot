#pragma once

#include <filesystem>
#include <string>
#include <vector>

#include "muse/levels.hpp"

namespace cgmusic::muse {

struct CalibrateOptions {
  std::string samples_dir;
  std::string library_path;
  std::string soundfont_dir;
};

// 执行全量测量标定:对映射可选取的所有 Muse 采样器乐器和 FluidSynth 回退乐器渲染标准测试乐句
std::vector<LevelCalibration> run_calibration(const CalibrateOptions& options);

// 输出 C++ 源码 (供 levels.cpp)
std::string generate_levels_cpp(const std::vector<LevelCalibration>& cal);

// 输出 JSON 字符串
std::string format_calibration_json(const std::vector<LevelCalibration>& cal);

}  // namespace cgmusic::muse
