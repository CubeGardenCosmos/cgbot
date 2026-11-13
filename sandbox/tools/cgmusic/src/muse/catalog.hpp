#pragma once

#include <filesystem>
#include <string>
#include <vector>

namespace cgmusic::muse {
struct InstrumentEntry;
}

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

// 与已许可/接受的乐器 ID 表取交集。仅保留 ID 包含在 accepted 中的乐器。
Catalog intersect_catalog(const Catalog& catalog, const std::vector<int>& accepted_ids);
Catalog intersect_catalog(const Catalog& catalog, const std::vector<muse::InstrumentEntry>& accepted);

}  // namespace cgmusic
