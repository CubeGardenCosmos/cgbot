#include <sqlite3.h>

#include <chrono>
#include <cstddef>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <map>
#include <optional>
#include <stdexcept>
#include <string>
#include <vector>

#include "muse/catalog.hpp"
#include "muse/mapping.hpp"
#include "test_harness.hpp"

namespace {

namespace fs = std::filesystem;

// 每个测试一个临时目录(遵循 TMPDIR),析构时删除。
class TempDir {
 public:
  TempDir() {
    const auto stamp = std::chrono::steady_clock::now().time_since_epoch().count();
    path_ = fs::temp_directory_path() / ("cgmusic-muse-test-" + std::to_string(stamp));
    fs::create_directories(path_);
  }
  ~TempDir() {
    std::error_code ec;
    fs::remove_all(path_, ec);
  }
  TempDir(const TempDir&) = delete;
  TempDir& operator=(const TempDir&) = delete;

  const fs::path& path() const { return path_; }

 private:
  fs::path path_;
};

class Db {
 public:
  explicit Db(const fs::path& file) {
    if (sqlite3_open(file.string().c_str(), &db_) != SQLITE_OK) {
      const std::string message = sqlite3_errmsg(db_);
      sqlite3_close(db_);
      throw std::runtime_error("sqlite open: " + message);
    }
  }
  ~Db() { sqlite3_close(db_); }
  Db(const Db&) = delete;
  Db& operator=(const Db&) = delete;

  void exec(const char* sql) {
    char* error = nullptr;
    if (sqlite3_exec(db_, sql, nullptr, nullptr, &error) != SQLITE_OK) {
      const std::string message = error != nullptr ? error : "unknown";
      sqlite3_free(error);
      throw std::runtime_error("sqlite exec: " + message);
    }
  }

 private:
  sqlite3* db_ = nullptr;
};

// 创建一个已安装的采样目录(含一个普通文件),使乐器通过采样可用性检查。
void make_samples(const fs::path& root, const std::string& relative) {
  const fs::path dir = root / relative;
  fs::create_directories(dir);
  std::ofstream(dir / "sample.sfz") << "<region>\n";
}

// 与真实 .instruments 相同的表结构;只插入测试数据。
// 91 已安装且有采样;92 已安装但无采样目录;166 采样存在但 state=0(downloading);
// 110 为 "Trumpet X"(缩写 Tpt.,用于缩写回退)。
void build_fixture(const fs::path& root) {
  make_samples(root, "Muse Strings/Violins 1");
  make_samples(root, "Muse Keys/Celesta");
  make_samples(root, "Muse Keys/Piano");
  make_samples(root, "Muse Percussion/GM Kit");
  make_samples(root, "Muse Music Box/Jewellery Music Box");
  make_samples(root, "Muse Brass/Trumpet X");

  Db db(root / ".instruments");
  db.exec(
      "CREATE TABLE metadata (key text PRIMARY KEY, value text);"
      "CREATE TABLE state (id integer PRIMARY KEY, name text);"
      "CREATE TABLE packages (key integer PRIMARY KEY, guid text not null, path text not null, "
      "name text);"
      "CREATE TABLE instruments (id integer PRIMARY KEY, path text not null, state integer, "
      "version text, package_key integer, name text, abbreviation text);"
      "INSERT INTO state VALUES (0, 'downloading'), (1, 'installed');"
      "INSERT INTO packages VALUES (1, 'g-strings', 'Muse Strings', 'Muse Strings'),"
      " (2, 'g-perc', 'Muse Percussion', 'Muse Percussion'),"
      " (3, 'g-keys', 'Muse Keys', 'Muse Keys'),"
      " (4, 'g-box', 'Muse Music Box', 'Muse Music Box'),"
      " (5, 'g-brass', 'Muse Brass', 'Muse Brass');"
      "INSERT INTO instruments (id, path, state, version, package_key, name, abbreviation) VALUES"
      " (91, 'Muse Strings/Violins 1', 1, '1', 1, ' Violins 1', 'Vln. 1'),"
      " (92, 'Muse Strings/Violins 2', 1, '1', 1, 'Violins 2', 'Vlns. 2'),"
      " (110, 'Muse Brass/Trumpet X', 1, '1', 5, 'Trumpet X', 'Tpt.'),"
      " (166, 'Muse Keys/Celesta', 0, '1', 3, 'Celesta', 'Cel.'),"
      " (167, 'Muse Keys/Piano', 1, '1', 3, 'Piano', 'Pno.'),"
      " (188, 'Muse Percussion/GM Kit', 1, '1', 2, 'GM Kit', 'Perc.'),"
      " (207, 'Muse Music Box/Jewellery Music Box', 1, '1', 4, 'Jewellery Music Box', 'M. Box');");
}

}  // namespace

CGMUSIC_TEST(catalog_keeps_installed_instruments_with_samples_in_id_order) {
  TempDir temp;
  build_fixture(temp.path());
  const cgmusic::Catalog catalog = cgmusic::load_catalog(temp.path());

  // 排除:92(无采样目录)、166(state=0)。
  CGMUSIC_CHECK(catalog.size() == 5);
  const std::vector<int> expected_ids{91, 110, 167, 188, 207};
  for (std::size_t i = 0; i < expected_ids.size() && i < catalog.size(); ++i) {
    CGMUSIC_CHECK(catalog[i].id == expected_ids[i]);
  }
  CGMUSIC_CHECK(catalog[0].name == "Violins 1");  // 前导空格已去除
  CGMUSIC_CHECK(catalog[0].abbreviation == "Vln. 1");
  CGMUSIC_CHECK(catalog[0].package == "Muse Strings");
  CGMUSIC_CHECK(catalog[0].family == "strings");
  CGMUSIC_CHECK(catalog[1].family == "brass");
  CGMUSIC_CHECK(catalog[2].family == "keys");
  CGMUSIC_CHECK(catalog[3].family == "percussion");
  CGMUSIC_CHECK(catalog[4].family == "other");  // Muse Music Box 不属于任何声部家族
}

CGMUSIC_TEST(catalog_missing_directory_is_empty_and_loads_are_deterministic) {
  TempDir temp;
  CGMUSIC_CHECK(cgmusic::load_catalog(temp.path() / "missing").empty());
  build_fixture(temp.path());
  const cgmusic::Catalog first = cgmusic::load_catalog(temp.path());
  const cgmusic::Catalog second = cgmusic::load_catalog(temp.path());
  CGMUSIC_CHECK(first.size() == second.size());
  for (std::size_t i = 0; i < first.size() && i < second.size(); ++i) {
    CGMUSIC_CHECK(first[i].id == second[i].id && first[i].name == second[i].name);
  }
}

CGMUSIC_TEST(mapping_picks_first_installed_candidate_by_name) {
  TempDir temp;
  build_fixture(temp.path());
  const cgmusic::Catalog catalog = cgmusic::load_catalog(temp.path());

  CGMUSIC_CHECK(cgmusic::pick_instrument(0, false, catalog) == 167);   // Piano
  CGMUSIC_CHECK(cgmusic::pick_instrument(40, false, catalog) == 91);   // Violins 1(Solo 未安装)
  CGMUSIC_CHECK(cgmusic::pick_instrument(0, true, catalog) == 188);    // GM Kit(鼓组通道)
  CGMUSIC_CHECK(cgmusic::pick_instrument(40, true, catalog) == 188);   // 鼓组忽略 GM 程序号
}

CGMUSIC_TEST(mapping_uses_unique_abbreviation_when_name_is_absent) {
  TempDir temp;
  build_fixture(temp.path());
  const cgmusic::Catalog catalog = cgmusic::load_catalog(temp.path());

  // 目录中无 "Trumpet",但 "Trumpet X" 的缩写 Tpt. 唯一,作为回退。
  CGMUSIC_CHECK(cgmusic::pick_instrument(56, false, catalog) == 110);
}

CGMUSIC_TEST(mapping_returns_nullopt_without_installed_candidate) {
  TempDir temp;
  build_fixture(temp.path());
  const cgmusic::Catalog catalog = cgmusic::load_catalog(temp.path());

  CGMUSIC_CHECK(!cgmusic::pick_instrument(73, false, catalog).has_value());  // Flute 1 未安装
  CGMUSIC_CHECK(!cgmusic::pick_instrument(0, false, cgmusic::Catalog{}).has_value());
  CGMUSIC_CHECK(!cgmusic::pick_instrument(0, true, cgmusic::Catalog{}).has_value());
}

CGMUSIC_TEST(electric_piano_programs_stay_on_fluidsynth) {
  TempDir temp;
  build_fixture(temp.path());
  const cgmusic::Catalog catalog = cgmusic::load_catalog(temp.path());

  // 目录里有钢琴,但 GM 4、5(电钢琴)保留给 FluidSynth 的 DX7 音色。
  CGMUSIC_CHECK(cgmusic::pick_instrument(0, false, catalog).has_value());
  CGMUSIC_CHECK(!cgmusic::pick_instrument(4, false, catalog).has_value());
  CGMUSIC_CHECK(!cgmusic::pick_instrument(5, false, catalog).has_value());
}

// 可选检查:仅当 MUSE_SAMPLES_DIR 设置时加载真实目录并打印数量;未设置则跳过。
CGMUSIC_TEST(real_muse_catalog_counts_when_samples_dir_set) {
  const char* root = std::getenv("MUSE_SAMPLES_DIR");
  if (root == nullptr || *root == '\0') {
    std::printf("SKIP real Muse catalog: MUSE_SAMPLES_DIR unset\n");
    return;
  }
  const cgmusic::Catalog catalog = cgmusic::load_catalog(root);
  std::map<std::string, std::size_t> by_family;
  bool has_gm_kit = false;
  for (const auto& instrument : catalog) {
    ++by_family[instrument.family];
    has_gm_kit = has_gm_kit || instrument.name == "GM Kit";
  }
  std::printf("real Muse catalog: %zu installed instruments\n", catalog.size());
  for (const auto& [family, count] : by_family) {
    std::printf("  family %-12s %zu\n", family.c_str(), count);
  }
  std::printf("  GM Kit present: %s\n", has_gm_kit ? "yes" : "no");
  if (catalog.size() < 200) {
    std::printf("NOTE: expected >= 200 installed instruments, found %zu\n", catalog.size());
  }
  CGMUSIC_CHECK(!catalog.empty());
  CGMUSIC_CHECK(has_gm_kit);
}
