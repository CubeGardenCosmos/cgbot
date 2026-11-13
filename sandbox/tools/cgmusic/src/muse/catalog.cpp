#include "muse/catalog.hpp"

#include <algorithm>
#include <cctype>
#include <cstdio>
#include <string_view>
#include <system_error>
#include <tuple>

#include <sqlite3.h>

#include "common.hpp"

namespace cgmusic {
namespace fs = std::filesystem;
namespace {

constexpr std::string_view kCatalogFileName = ".instruments";
constexpr std::string_view kInstalledState = "installed";  // state 表中的名称

// instruments.state 经 state 表解析为名称,只取已安装项。
constexpr const char* kInstrumentQuery =
    "SELECT i.id, i.name, i.abbreviation, i.path, p.name "
    "FROM instruments i "
    "JOIN state s ON s.id = i.state "
    "LEFT JOIN packages p ON p.key = i.package_key "
    "WHERE s.name = ?1 ORDER BY i.id";

struct FamilyRule {
  std::string_view prefix;  // 包名归一化后的前缀
  std::string_view family;
};

// 包名到声部家族;未列出的包归入 other。
constexpr FamilyRule kFamilyRules[] = {
    {"strings", "strings"},       {"woodwinds", "woodwinds"}, {"brass", "brass"},
    {"percussion", "percussion"}, {"drumline", "drumline"},   {"keys", "keys"},
    {"guitars", "guitars"},       {"choir", "choir"},         {"harp", "harp"},
    {"handbells", "handbells"},
};

// 去掉 "Muse " 前缀,仅保留小写字母与数字:"Muse Guitars Vol. 1" -> "guitarsvol1"。
std::string normalize_package(std::string_view package) {
  constexpr std::string_view kPrefix = "Muse ";
  if (package.substr(0, kPrefix.size()) == kPrefix) package.remove_prefix(kPrefix.size());
  std::string key;
  for (const char c : package) {
    if (std::isalnum(static_cast<unsigned char>(c)) != 0) {
      key.push_back(static_cast<char>(std::tolower(static_cast<unsigned char>(c))));
    }
  }
  return key;
}

std::string family_of(std::string_view package) {
  const std::string key = normalize_package(package);
  for (const FamilyRule& rule : kFamilyRules) {
    if (key.compare(0, rule.prefix.size(), rule.prefix) == 0) return std::string(rule.family);
  }
  return "other";
}

// 把路径编码为只读、不加锁的 file: URI,使 ? # % 与空格按字面处理。
std::string readonly_uri(const fs::path& file) {
  std::string uri = "file:";
  for (const unsigned char c : file.generic_string()) {
    if (std::isalnum(c) != 0 || c == '/' || c == '-' || c == '_' || c == '.' || c == '~') {
      uri.push_back(static_cast<char>(c));
    } else {
      char escaped[4];
      std::snprintf(escaped, sizeof escaped, "%%%02X", static_cast<unsigned>(c));
      uri += escaped;
    }
  }
  return uri + "?mode=ro&immutable=1";
}

class Database {
 public:
  explicit Database(const fs::path& file) {
    const int rc = sqlite3_open_v2(readonly_uri(fs::absolute(file)).c_str(), &db_,
                                   SQLITE_OPEN_READONLY | SQLITE_OPEN_URI, nullptr);
    if (rc != SQLITE_OK) {
      const std::string message = db_ != nullptr ? sqlite3_errmsg(db_) : "out of memory";
      sqlite3_close(db_);
      throw Error("bad_input", "Cannot open Muse catalog " + file.string() + ": " + message);
    }
  }
  ~Database() { sqlite3_close(db_); }
  Database(const Database&) = delete;
  Database& operator=(const Database&) = delete;

  sqlite3* handle() const noexcept { return db_; }

 private:
  sqlite3* db_ = nullptr;
};

class Statement {
 public:
  Statement(sqlite3* db, const char* sql) {
    if (sqlite3_prepare_v2(db, sql, -1, &stmt_, nullptr) != SQLITE_OK) {
      const std::string message = sqlite3_errmsg(db);
      sqlite3_finalize(stmt_);
      throw Error("bad_input", "Muse catalog query failed: " + message);
    }
  }
  ~Statement() { sqlite3_finalize(stmt_); }
  Statement(const Statement&) = delete;
  Statement& operator=(const Statement&) = delete;

  sqlite3_stmt* get() const noexcept { return stmt_; }

 private:
  sqlite3_stmt* stmt_ = nullptr;
};

// 读取文本列并去掉首尾空白(目录库中名称存在前导空格,如 " Violins 1")。
std::string text_column(sqlite3_stmt* stmt, int column) {
  const unsigned char* text = sqlite3_column_text(stmt, column);
  if (text == nullptr) return {};
  const std::string value(reinterpret_cast<const char*>(text));
  const auto first = value.find_first_not_of(" \t\r\n");
  if (first == std::string::npos) return {};
  const auto last = value.find_last_not_of(" \t\r\n");
  return value.substr(first, last - first + 1);
}

// 采样可用:目录 root/relative 存在且其中至少有一个普通文件。
bool samples_installed(const fs::path& root, const std::string& relative) {
  if (relative.empty()) return false;
  const fs::path dir = root / relative;
  std::error_code ec;
  if (!fs::is_directory(dir, ec)) return false;
  for (fs::recursive_directory_iterator it(dir, ec), end; !ec && it != end; it.increment(ec)) {
    std::error_code file_ec;
    if (it->is_regular_file(file_ec)) return true;
  }
  return false;
}

std::vector<fs::path> find_catalog_files(const fs::path& dir) {
  std::vector<fs::path> files;
  std::error_code ec;
  const auto options = fs::directory_options::skip_permission_denied;
  for (fs::recursive_directory_iterator it(dir, options, ec), end; !ec && it != end;
       it.increment(ec)) {
    std::error_code file_ec;
    const std::string name = it->path().filename().string();
    const bool suffix_match =
        name.size() >= kCatalogFileName.size() &&
        name.compare(name.size() - kCatalogFileName.size(), kCatalogFileName.size(),
                     kCatalogFileName) == 0;
    if (suffix_match && it->is_regular_file(file_ec)) files.push_back(it->path());
  }
  if (ec) {
    throw Error("bad_input", "Cannot scan Muse sample root " + dir.string() + ": " + ec.message());
  }
  std::sort(files.begin(), files.end());
  return files;
}

}  // namespace

Catalog load_catalog(const fs::path& dir) {
  Catalog catalog;
  std::error_code ec;
  if (!fs::is_directory(dir, ec)) return catalog;

  for (const fs::path& file : find_catalog_files(dir)) {
    const Database db(file);
    const Statement stmt(db.handle(), kInstrumentQuery);
    sqlite3_bind_text(stmt.get(), 1, kInstalledState.data(),
                      static_cast<int>(kInstalledState.size()), SQLITE_STATIC);
    const fs::path root = file.parent_path();
    int rc = SQLITE_ERROR;
    while ((rc = sqlite3_step(stmt.get())) == SQLITE_ROW) {
      if (!samples_installed(root, text_column(stmt.get(), 3))) continue;
      MuseInstrument instrument;
      instrument.id = sqlite3_column_int(stmt.get(), 0);
      instrument.name = text_column(stmt.get(), 1);
      instrument.abbreviation = text_column(stmt.get(), 2);
      instrument.package = text_column(stmt.get(), 4);
      instrument.family = family_of(instrument.package);
      catalog.push_back(std::move(instrument));
    }
    if (rc != SQLITE_DONE) {
      throw Error("bad_input", "Muse catalog read failed in " + file.string() + ": " +
                                   sqlite3_errmsg(db.handle()));
    }
  }

  std::stable_sort(catalog.begin(), catalog.end(), [](const MuseInstrument& a,
                                                      const MuseInstrument& b) {
    return std::tie(a.id, a.package, a.name) < std::tie(b.id, b.package, b.name);
  });
  return catalog;
}

}  // namespace cgmusic
