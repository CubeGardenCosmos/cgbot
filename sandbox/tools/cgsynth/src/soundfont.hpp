#pragma once

#include <cstdint>
#include <filesystem>
#include <string>
#include <string_view>
#include <vector>

#include "common.hpp"

namespace cgmusic {

struct SoundfontInfo {
  std::string id;  // 小写文件主名
  std::string file_name;
  std::filesystem::path path;
  std::uintmax_t bytes = 0;
  bool is_default = false;
};

// 环境变量 CGMUSIC_SOUNDFONTS 优先,默认 /opt/soundfonts。
std::filesystem::path soundfont_dir();

// 只读扫描 .sf3/.sf2;目录不存在时返回空列表。按 id 排序,同 id 时 sf3 在前。
std::vector<SoundfontInfo> scan_soundfonts(const std::filesystem::path& dir);

// 默认音色:MuseScore_General* 中最大者;否则最大的 sf3;否则最大的 sf2。
SoundfontInfo default_soundfont(const std::filesystem::path& dir);

// spec 为 id(大小写不敏感)或existing 的 .sf2/.sf3 文件路径。
SoundfontInfo resolve_soundfont(const std::filesystem::path& dir, std::string_view spec);

}  // namespace cgmusic
