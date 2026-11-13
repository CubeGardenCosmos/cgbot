#include "soundfont.hpp"

#include <algorithm>
#include <cctype>
#include <cstdlib>
#include <optional>
#include <system_error>

namespace fs = std::filesystem;

namespace cgmusic {
namespace {

constexpr const char* kDefaultDir = "/opt/soundfonts";
constexpr const char* kDefaultPrefix = "musescore_general";

std::string lower(std::string text) {
  std::transform(text.begin(), text.end(), text.begin(),
                 [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
  return text;
}

bool is_sf3(const fs::path& path) { return lower(path.extension().string()) == ".sf3"; }

bool is_soundfont(const fs::path& path) {
  const std::string ext = lower(path.extension().string());
  return ext == ".sf2" || ext == ".sf3";
}

bool has_prefix(const std::string& text, const std::string& prefix) {
  return text.size() >= prefix.size() && text.compare(0, prefix.size(), prefix) == 0;
}

SoundfontInfo describe(const fs::path& path) {
  SoundfontInfo info;
  info.id = lower(path.stem().string());
  info.file_name = path.filename().string();
  info.path = path;
  std::error_code ec;
  const auto size = fs::file_size(path, ec);
  info.bytes = ec ? 0 : size;
  return info;
}

}  // namespace

fs::path soundfont_dir() {
  const char* env = std::getenv("CGMUSIC_SOUNDFONTS");
  if (env != nullptr && *env != '\0') return fs::path(env);
  return fs::path(kDefaultDir);
}

std::vector<SoundfontInfo> scan_soundfonts(const fs::path& dir) {
  std::vector<SoundfontInfo> fonts;
  std::error_code ec;
  if (!fs::is_directory(dir, ec)) return fonts;

  fs::directory_iterator it(dir, ec);
  if (ec) throw Error("bad_input", "Cannot read the SoundFont directory");
  for (const fs::directory_iterator end; it != end; it.increment(ec)) {
    if (ec) throw Error("bad_input", "Cannot read the SoundFont directory");
    if (!is_soundfont(it->path())) continue;
    std::error_code entry_ec;
    if (!it->is_regular_file(entry_ec) || entry_ec) continue;
    fonts.push_back(describe(it->path()));
  }

  std::sort(fonts.begin(), fonts.end(), [](const SoundfontInfo& a, const SoundfontInfo& b) {
    if (a.id != b.id) return a.id < b.id;
    return is_sf3(a.path) && !is_sf3(b.path);
  });

  std::optional<std::size_t> chosen;
  const auto consider = [&](std::size_t index) {
    if (!chosen || fonts[index].bytes > fonts[*chosen].bytes) chosen = index;
  };
  for (std::size_t i = 0; i < fonts.size(); ++i) {
    if (has_prefix(fonts[i].id, kDefaultPrefix)) consider(i);
  }
  if (!chosen) {
    for (std::size_t i = 0; i < fonts.size(); ++i) {
      if (is_sf3(fonts[i].path)) consider(i);
    }
  }
  if (!chosen) {
    for (std::size_t i = 0; i < fonts.size(); ++i) consider(i);
  }
  if (chosen) fonts[*chosen].is_default = true;
  return fonts;
}

SoundfontInfo default_soundfont(const fs::path& dir) {
  for (auto& font : scan_soundfonts(dir)) {
    if (font.is_default) return font;
  }
  throw Error("no_soundfont", "No SoundFont is installed in " + dir.string());
}

SoundfontInfo resolve_soundfont(const fs::path& dir, std::string_view spec) {
  const std::string wanted = lower(std::string(spec));
  for (auto& font : scan_soundfonts(dir)) {
    if (font.id == wanted) return font;
  }
  const fs::path candidate{std::string(spec)};
  std::error_code ec;
  if (is_soundfont(candidate) && fs::is_regular_file(candidate, ec) && !ec) {
    return describe(candidate);
  }
  throw Error("bad_input", "Unknown SoundFont: " + std::string(spec));
}

}  // namespace cgmusic
