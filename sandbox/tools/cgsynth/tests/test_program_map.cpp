#include <functional>
#include <set>
#include <string>

#include "common.hpp"
#include "program_map.hpp"
#include "test_harness.hpp"

namespace {

std::string error_code_of(const std::function<void()>& action) {
  try {
    action();
  } catch (const cgmusic::Error& error) {
    return error.code();
  }
  return "none";
}

}  // namespace

CGMUSIC_TEST(exact_preset_is_kept) {
  const std::set<cgmusic::PresetKey> available{{0, 0}, {0, 48}, {128, 0}};
  const cgmusic::PresetKey hit = cgmusic::resolve_preset(available, {0, 48});
  CGMUSIC_CHECK(hit.bank == 0 && hit.program == 48);
}

CGMUSIC_TEST(missing_program_uses_nearest_in_same_bank) {
  const std::set<cgmusic::PresetKey> available{{0, 44}, {0, 48}, {0, 70}};
  const cgmusic::PresetKey near = cgmusic::resolve_preset(available, {0, 45});
  CGMUSIC_CHECK(near.bank == 0 && near.program == 44);
  // 46 与 44、48 等距,取较小者。
  const cgmusic::PresetKey tie = cgmusic::resolve_preset(available, {0, 46});
  CGMUSIC_CHECK(tie.program == 44);
}

CGMUSIC_TEST(missing_bank_falls_back_to_melodic_bank) {
  const std::set<cgmusic::PresetKey> available{{0, 0}, {0, 73}};
  const cgmusic::PresetKey melodic = cgmusic::resolve_preset(available, {8, 10});
  CGMUSIC_CHECK(melodic.bank == 0 && melodic.program == 0);
}

CGMUSIC_TEST(missing_drum_kit_falls_back_to_melodic_bank) {
  const std::set<cgmusic::PresetKey> available{{0, 0}, {0, 56}};
  const cgmusic::PresetKey fallback = cgmusic::resolve_preset(available, {128, 0});
  CGMUSIC_CHECK(fallback.bank == 0 && fallback.program == 0);
}

CGMUSIC_TEST(empty_soundfont_has_no_preset) {
  const std::set<cgmusic::PresetKey> none;
  CGMUSIC_CHECK(error_code_of([&] { cgmusic::resolve_preset(none, {0, 0}); }) == "no_soundfont");
}
