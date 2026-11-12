#include <filesystem>
#include <fstream>
#include <functional>
#include <string>

#include "common.hpp"
#include "soundfont.hpp"
#include "test_harness.hpp"

namespace fs = std::filesystem;

namespace {

fs::path fresh_dir(const std::string& name) {
  const fs::path dir = fs::temp_directory_path() / ("cgmusic_test_" + name);
  fs::remove_all(dir);
  fs::create_directories(dir);
  return dir;
}

void write_bytes(const fs::path& path, std::size_t size) {
  std::ofstream out(path, std::ios::binary);
  const std::string payload(size, 'x');
  out << payload;
}

std::string error_code_of(const std::function<void()>& action) {
  try {
    action();
  } catch (const cgmusic::Error& error) {
    return error.code();
  }
  return "none";
}

}  // namespace

CGMUSIC_TEST(scan_lists_fonts_sorted_with_musescore_default) {
  const fs::path dir = fresh_dir("scan");
  write_bytes(dir / "MuseScore_General.sf3", 100);
  write_bytes(dir / "Other_Font.sf2", 500);
  write_bytes(dir / "Tiny.sf3", 50);
  write_bytes(dir / "readme.txt", 10);

  const auto fonts = cgmusic::scan_soundfonts(dir);
  CGMUSIC_CHECK(fonts.size() == 3);
  CGMUSIC_CHECK(fonts[0].id == "musescore_general");
  CGMUSIC_CHECK(fonts[0].file_name == "MuseScore_General.sf3");
  CGMUSIC_CHECK(fonts[0].bytes == 100);
  CGMUSIC_CHECK(fonts[1].id == "other_font");
  CGMUSIC_CHECK(fonts[2].id == "tiny");
  CGMUSIC_CHECK(fonts[0].is_default);
  CGMUSIC_CHECK(!fonts[1].is_default);
  CGMUSIC_CHECK(!fonts[2].is_default);
  CGMUSIC_CHECK(cgmusic::default_soundfont(dir).id == "musescore_general");
}

CGMUSIC_TEST(default_falls_back_to_largest_sf3_without_musescore) {
  const fs::path dir = fresh_dir("fallback");
  write_bytes(dir / "Tiny.sf3", 50);
  write_bytes(dir / "Big.sf3", 300);
  write_bytes(dir / "Other_Font.sf2", 900);
  CGMUSIC_CHECK(cgmusic::default_soundfont(dir).id == "big");
}

CGMUSIC_TEST(resolve_accepts_id_case_insensitively_and_rejects_unknown) {
  const fs::path dir = fresh_dir("resolve");
  write_bytes(dir / "Other_Font.sf2", 500);
  CGMUSIC_CHECK(cgmusic::resolve_soundfont(dir, "OTHER_FONT").id == "other_font");
  CGMUSIC_CHECK(error_code_of([&] { cgmusic::resolve_soundfont(dir, "missing"); }) == "bad_input");
}

CGMUSIC_TEST(missing_or_empty_directory_has_no_default) {
  const fs::path missing = fs::temp_directory_path() / "cgmusic_test_does_not_exist";
  fs::remove_all(missing);
  CGMUSIC_CHECK(cgmusic::scan_soundfonts(missing).empty());
  CGMUSIC_CHECK(error_code_of([&] { cgmusic::default_soundfont(missing); }) == "no_soundfont");
}
