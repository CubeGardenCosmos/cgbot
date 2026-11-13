#include <cstdint>
#include <functional>
#include <set>
#include <string>

#include "common.hpp"
#include "muse/abi.hpp"
#include "muse/loader.hpp"
#include "test_harness.hpp"

namespace {

using cgmusic::muse::Api;
using cgmusic::muse::InitKind;
using cgmusic::muse::Version;

std::string error_code_of(const std::function<void()>& action) {
  try {
    action();
  } catch (const cgmusic::Error& error) {
    return error.code();
  }
  return "none";
}

Version version_of(int major, int minor) {
  Version version;
  version.major = major;
  version.minor = minor;
  version.revision = 0;
  version.text = std::to_string(major) + "." + std::to_string(minor) + ".0";
  return version;
}

// 假 dlsym:只认 present 里的名字,地址取每个名字的唯一哨兵。
cgmusic::muse::SymbolLookup fake_lookup(const std::set<std::string>& present) {
  return [present](const char* name) -> void* {
    if (present.count(name) == 0) return nullptr;
    // 以名字长度与首字符构造一个非空、各名互异不要求的地址;测试只比较"非空"与指定符号。
    static char sentinel[256];
    return &sentinel[static_cast<unsigned char>(name[0])];
  };
}

std::set<std::string> all_required_names() {
  std::set<std::string> names;
#define CGMUSIC_TEST_COLLECT(name) names.insert(#name);
  CGMUSIC_MUSE_REQUIRED_SYMBOLS(CGMUSIC_TEST_COLLECT)
#undef CGMUSIC_TEST_COLLECT
  return names;
}

}  // namespace

CGMUSIC_TEST(muse_abi_layout_is_pinned) {
  using namespace cgmusic::muse::abi;
  CGMUSIC_CHECK(sizeof(ms_NoteEvent_5) == 72);
  CGMUSIC_CHECK(sizeof(ms_DynamicsEvent_2) == 16);
  CGMUSIC_CHECK(sizeof(ms_OutputBuffer) == 16);
  CGMUSIC_CHECK(offsetof(ms_NoteEvent_5, _notehead) == 64);
}

CGMUSIC_TEST(muse_version_gate_accepts_window_and_rejects_outside) {
  CGMUSIC_CHECK(error_code_of([] { cgmusic::muse::check_version(version_of(0, 102)); }) == "none");
  CGMUSIC_CHECK(error_code_of([] { cgmusic::muse::check_version(version_of(0, 106)); }) == "none");
  CGMUSIC_CHECK(error_code_of([] { cgmusic::muse::check_version(version_of(0, 150)); }) == "none");
  CGMUSIC_CHECK(error_code_of([] { cgmusic::muse::check_version(version_of(0, 101)); }) == "muse_version");
  CGMUSIC_CHECK(error_code_of([] { cgmusic::muse::check_version(version_of(0, 6)); }) == "muse_version");
  CGMUSIC_CHECK(error_code_of([] { cgmusic::muse::check_version(version_of(1, 0)); }) == "muse_version");
}

CGMUSIC_TEST(muse_init_selection_switches_at_0_106) {
  CGMUSIC_CHECK(cgmusic::muse::select_init(version_of(0, 105)) == InitKind::Init2);
  CGMUSIC_CHECK(cgmusic::muse::select_init(version_of(0, 106)) == InitKind::Init3);
  CGMUSIC_CHECK(cgmusic::muse::select_init(version_of(0, 107)) == InitKind::Init3);
}

CGMUSIC_TEST(muse_bind_tolerates_missing_optional_symbols) {
  const std::set<std::string> present = all_required_names();  // 可选符号一个都没有
  std::set<std::string> with_init3 = present;
  with_init3.insert("ms_init_3");
  Api api;
  const auto missing = cgmusic::muse::bind_symbols(fake_lookup(with_init3), api, version_of(0, 106));
  CGMUSIC_CHECK(missing.empty());
  CGMUSIC_CHECK(api.ms_init_3 != nullptr);
  CGMUSIC_CHECK(api.ms_init_2 == nullptr);
  CGMUSIC_CHECK(api.ms_MuseSampler_process_offline != nullptr);
  CGMUSIC_CHECK(api.ms_get_version_string == nullptr);
  CGMUSIC_CHECK(api.ms_Instrument_get_reverb_level == nullptr);
}

CGMUSIC_TEST(muse_bind_reports_missing_required_symbols) {
  std::set<std::string> present = all_required_names();
  present.erase("ms_MuseSampler_process_offline");
  present.erase("ms_MuseSampler_create");
  present.insert("ms_init_3");
  Api api;
  const auto missing = cgmusic::muse::bind_symbols(fake_lookup(present), api, version_of(0, 106));
  CGMUSIC_CHECK(missing.size() == 2);
  CGMUSIC_CHECK(api.ms_MuseSampler_process_offline == nullptr);
  CGMUSIC_CHECK(api.ms_MuseSampler_add_track != nullptr);
}

CGMUSIC_TEST(muse_bind_requires_init_matching_version) {
  std::set<std::string> present = all_required_names();
  present.insert("ms_init_2");  // 只有 init_2
  Api old_api;
  CGMUSIC_CHECK(cgmusic::muse::bind_symbols(fake_lookup(present), old_api, version_of(0, 105)).empty());
  CGMUSIC_CHECK(old_api.ms_init_2 != nullptr);
  Api new_api;
  const auto missing = cgmusic::muse::bind_symbols(fake_lookup(present), new_api, version_of(0, 106));
  CGMUSIC_CHECK(missing.size() == 1);
  CGMUSIC_CHECK(missing.front() == "ms_init_3");
}

CGMUSIC_TEST(muse_version_symbols_are_all_required) {
  Api api;
  const auto missing = cgmusic::muse::bind_version_symbols(fake_lookup({"ms_get_version_major"}), api);
  CGMUSIC_CHECK(missing.size() == 2);
  CGMUSIC_CHECK(api.ms_get_version_major != nullptr);
  CGMUSIC_CHECK(api.ms_get_version_minor == nullptr);
}

CGMUSIC_TEST(muse_load_missing_library_is_unavailable) {
  CGMUSIC_CHECK(error_code_of([] { cgmusic::muse::MuseLib::load("/nonexistent/libMuseSamplerCoreLib.so"); }) ==
                "muse_unavailable");
}
