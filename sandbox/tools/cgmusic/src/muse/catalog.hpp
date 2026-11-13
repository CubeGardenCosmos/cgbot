#pragma once

#include <filesystem>
#include <string>
#include <vector>

namespace cgmusic {

struct MuseInstrument {
  int id = 0;
  std::string name;
  std::string abbreviation;
  std::string package;
  std::string family;
};

using Catalog = std::vector<MuseInstrument>;

// 读取目录及其子目录中的 .instruments;仅保留已安装且采样目录存在的乐器。
// 找不到目录或目录中没有目录库时返回空表;损坏的目录库抛出 Error。
Catalog load_catalog(const std::filesystem::path& dir);

}  // namespace cgmusic
