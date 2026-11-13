#include "muse/loader.hpp"

#include <dlfcn.h>

#include <cstdlib>
#include <utility>

#include "common.hpp"

namespace cgmusic::muse {

namespace {

template <typename Fn>
Fn lookup_as(const SymbolLookup& lookup, const char* name) {
  return reinterpret_cast<Fn>(lookup(name));
}

std::string join(const std::vector<std::string>& names) {
  std::string out;
  for (const std::string& name : names) {
    if (!out.empty()) out += ", ";
    out += name;
  }
  return out;
}

std::string text_or_empty(const char* value) { return value != nullptr ? value : ""; }

}  // namespace

std::vector<std::string> bind_version_symbols(const SymbolLookup& lookup, Api& api) {
  std::vector<std::string> missing;
  api.ms_get_version_major = lookup_as<abi::ms_get_version_major_fn>(lookup, "ms_get_version_major");
  api.ms_get_version_minor = lookup_as<abi::ms_get_version_minor_fn>(lookup, "ms_get_version_minor");
  api.ms_get_version_revision =
      lookup_as<abi::ms_get_version_revision_fn>(lookup, "ms_get_version_revision");
  if (api.ms_get_version_major == nullptr) missing.emplace_back("ms_get_version_major");
  if (api.ms_get_version_minor == nullptr) missing.emplace_back("ms_get_version_minor");
  if (api.ms_get_version_revision == nullptr) missing.emplace_back("ms_get_version_revision");
  return missing;
}

Version query_version(const Api& api) {
  Version version;
  version.major = api.ms_get_version_major();
  version.minor = api.ms_get_version_minor();
  version.revision = api.ms_get_version_revision();
  if (api.ms_get_version_build_number != nullptr) version.build = api.ms_get_version_build_number();
  if (api.ms_get_version_string != nullptr) version.text = text_or_empty(api.ms_get_version_string());
  if (version.text.empty()) {
    version.text = std::to_string(version.major) + "." + std::to_string(version.minor) + "." +
                   std::to_string(version.revision);
  }
  return version;
}

void check_version(const Version& version) {
  if (version.major != kSupportedMajor) {
    throw Error("muse_version", "MuseSampler " + version.text + " 的主版本不受支持(需要 " +
                                    std::to_string(kSupportedMajor) + ".x)");
  }
  if (version.minor < kMinimumMinor) {
    throw Error("muse_version", "MuseSampler " + version.text + " 过旧(最低 " +
                                    std::to_string(kSupportedMajor) + "." +
                                    std::to_string(kMinimumMinor) + ")");
  }
}

InitKind select_init(const Version& version) {
  return version.minor >= kInitThreeMinor ? InitKind::Init3 : InitKind::Init2;
}

std::vector<std::string> bind_symbols(const SymbolLookup& lookup, Api& api, const Version& version) {
  std::vector<std::string> missing;
#define CGMUSIC_MUSE_BIND_REQUIRED(name)                                \
  api.name = lookup_as<abi::name##_fn>(lookup, #name);                  \
  if (api.name == nullptr) missing.emplace_back(#name);
  CGMUSIC_MUSE_REQUIRED_SYMBOLS(CGMUSIC_MUSE_BIND_REQUIRED)
#undef CGMUSIC_MUSE_BIND_REQUIRED
#define CGMUSIC_MUSE_BIND_OPTIONAL(name) api.name = lookup_as<abi::name##_fn>(lookup, #name);
  CGMUSIC_MUSE_OPTIONAL_SYMBOLS(CGMUSIC_MUSE_BIND_OPTIONAL)
#undef CGMUSIC_MUSE_BIND_OPTIONAL

  // 两个 init 都尝试绑定,只有按版本选定的那个算必需。
  api.ms_init_2 = lookup_as<abi::ms_init_2_fn>(lookup, "ms_init_2");
  api.ms_init_3 = lookup_as<abi::ms_init_3_fn>(lookup, "ms_init_3");
  if (select_init(version) == InitKind::Init3) {
    if (api.ms_init_3 == nullptr) missing.emplace_back("ms_init_3");
  } else if (api.ms_init_2 == nullptr) {
    missing.emplace_back("ms_init_2");
  }
  return missing;
}

MuseLib MuseLib::load(const std::string& path) {
  // 库内部有后台线程与全局状态,不 dlclose;句柄随进程结束。
  void* handle = ::dlopen(path.c_str(), RTLD_NOW | RTLD_LOCAL);
  if (handle == nullptr) {
    const char* reason = ::dlerror();
    throw Error("muse_unavailable",
                "无法加载 MuseSampler 库 " + path + ":" + (reason != nullptr ? reason : "未知错误"));
  }
  const SymbolLookup lookup = [handle](const char* name) { return ::dlsym(handle, name); };

  MuseLib lib;
  const std::vector<std::string> missing_version = bind_version_symbols(lookup, lib.api_);
  if (!missing_version.empty()) {
    throw Error("muse_unavailable", "MuseSampler 库缺少版本符号:" + join(missing_version));
  }
  // 版本门要先于其余符号:旧库缺的符号不应被报成"缺符号"。
  lib.api_.ms_get_version_build_number = lookup_as<abi::ms_get_version_build_number_fn>(
      lookup, "ms_get_version_build_number");
  lib.api_.ms_get_version_string =
      lookup_as<abi::ms_get_version_string_fn>(lookup, "ms_get_version_string");
  lib.version_ = query_version(lib.api_);
  check_version(lib.version_);
  lib.init_kind_ = select_init(lib.version_);

  const std::vector<std::string> missing = bind_symbols(lookup, lib.api_, lib.version_);
  if (!missing.empty()) {
    throw Error("muse_unavailable", "MuseSampler " + lib.version_.text + " 缺少必需符号:" + join(missing));
  }
  return lib;
}

void MuseLib::init(const std::string& sample_root) const {
  if (!sample_root.empty() &&
      ::setenv("MUSESAMPLER_INSTRUMENT_FOLDER", sample_root.c_str(), 1) != 0) {
    throw Error("muse_unavailable", "无法设置 MUSESAMPLER_INSTRUMENT_FOLDER");
  }
  const abi::ms_Result init_result =
      init_kind_ == InitKind::Init3 ? api_.ms_init_3() : api_.ms_init_2();
  if (init_result != abi::kResultOk) {
    throw Error("muse_unavailable", std::string(init_kind_ == InitKind::Init3 ? "ms_init_3" : "ms_init_2") +
                                        " 失败,返回 " + std::to_string(init_result) +
                                        "(返回 -5 为样本根 .instruments 缺失;返回 -6 为 receipt/授权记录缺失,即 ~/.local/share/muse-sounds-manager/.receipt 不存在;返回 -1 原因未确定)");
  }
  const abi::ms_Result reverb_result = api_.ms_disable_reverb();
  if (reverb_result != abi::kResultOk) {
    throw Error("muse_unavailable", "ms_disable_reverb 失败,返回 " + std::to_string(reverb_result));
  }
}

std::vector<InstrumentEntry> MuseLib::list_instruments() const {
  std::vector<InstrumentEntry> entries;
  const abi::ms_InstrumentList list = api_.ms_get_instrument_list();
  if (list == nullptr) return entries;
  while (abi::ms_InstrumentInfo info = api_.ms_InstrumentList_get_next(list)) {
    InstrumentEntry entry;
    entry.id = api_.ms_Instrument_get_id(info);
    entry.name = text_or_empty(api_.ms_Instrument_get_name(info));
    entry.category = text_or_empty(api_.ms_Instrument_get_category(info));
    entry.package = text_or_empty(api_.ms_Instrument_get_package(info));
    entries.push_back(std::move(entry));
  }
  return entries;
}

}  // namespace cgmusic::muse
