#pragma once

#include <functional>
#include <string>
#include <vector>

#include "muse/abi.hpp"

namespace cgmusic::muse {

// 必需符号(版本无关)。X(导出符号名)。
// 版本读取三件套单独处理(见 kVersionSymbols),ms_init_2 / ms_init_3 按版本二选一(见 select_init)。
#define CGMUSIC_MUSE_REQUIRED_SYMBOLS(X) \
  X(ms_disable_reverb)                   \
  X(ms_get_instrument_list)              \
  X(ms_InstrumentList_get_next)          \
  X(ms_Instrument_get_id)                \
  X(ms_Instrument_get_name)              \
  X(ms_Instrument_get_category)          \
  X(ms_Instrument_get_package)           \
  X(ms_MuseSampler_create)               \
  X(ms_MuseSampler_destroy)              \
  X(ms_MuseSampler_init_2)               \
  X(ms_MuseSampler_add_track)            \
  X(ms_MuseSampler_finalize_track)       \
  X(ms_MuseSampler_add_track_note_event_6)      \
  X(ms_MuseSampler_add_track_dynamics_event_2)  \
  X(ms_MuseSampler_ready_to_play)        \
  X(ms_MuseSampler_trigger_render)       \
  X(ms_MuseSampler_start_offline_mode)   \
  X(ms_MuseSampler_stop_offline_mode)    \
  X(ms_MuseSampler_process_offline)

// 可选符号:缺失只置空,不报错。
#define CGMUSIC_MUSE_OPTIONAL_SYMBOLS(X) \
  X(ms_get_version_string)               \
  X(ms_get_version_build_number)         \
  X(ms_Instrument_get_pack_name)         \
  X(ms_Instrument_get_vendor_name)       \
  X(ms_Instrument_get_musicxml_sound)    \
  X(ms_Instrument_get_mpe_sound)         \
  X(ms_Instrument_get_reverb_level)      \
  X(ms_MuseSampler_clear_score)

// 受支持的版本窗口:主版本 0;最低 0.102(ms_NoteEvent_5 / note_event_6 / trigger_render 的引入版本)。
inline constexpr int kSupportedMajor = 0;
inline constexpr int kMinimumMinor = 102;
// 从该次版本起全局初始化改用 ms_init_3。
inline constexpr int kInitThreeMinor = 106;

struct Version {
  int major = -1;
  int minor = -1;
  int revision = -1;
  int build = -1;     // 缺 ms_get_version_build_number 时为 -1
  std::string text;   // 缺 ms_get_version_string 时由三段数字拼出
};

enum class InitKind { Init2, Init3 };

// 解析到的函数指针表。未解析的成员为 nullptr。
struct Api {
  abi::ms_get_version_major_fn ms_get_version_major = nullptr;
  abi::ms_get_version_minor_fn ms_get_version_minor = nullptr;
  abi::ms_get_version_revision_fn ms_get_version_revision = nullptr;
  abi::ms_init_2_fn ms_init_2 = nullptr;
  abi::ms_init_3_fn ms_init_3 = nullptr;
#define CGMUSIC_MUSE_DECLARE(name) abi::name##_fn name = nullptr;
  CGMUSIC_MUSE_REQUIRED_SYMBOLS(CGMUSIC_MUSE_DECLARE)
  CGMUSIC_MUSE_OPTIONAL_SYMBOLS(CGMUSIC_MUSE_DECLARE)
#undef CGMUSIC_MUSE_DECLARE
};

// dlsym 的可替换形式;找不到返回 nullptr。
using SymbolLookup = std::function<void*(const char* name)>;

// 阶段 1:只绑定版本三件套。缺失的符号名放入返回值。
std::vector<std::string> bind_version_symbols(const SymbolLookup& lookup, Api& api);
// 调用已绑定的版本函数。
Version query_version(const Api& api);
// 版本门:主版本必须为 0,次版本 >= kMinimumMinor;否则抛 Error("muse_version")。
void check_version(const Version& version);
// >= 0.106 用 ms_init_3,否则 ms_init_2。
InitKind select_init(const Version& version);
// 阶段 2:绑定其余符号(必需 + 可选 + 按版本选定的 init)。返回缺失的必需符号名;
// 可选符号缺失不计入。
std::vector<std::string> bind_symbols(const SymbolLookup& lookup, Api& api, const Version& version);

struct InstrumentEntry {
  int id = -1;
  std::string name;
  std::string category;
  std::string package;
};

// 已加载的采样器库。进程内持有至退出:库内部有后台线程,故析构时不 dlclose。
class MuseLib {
 public:
  // dlopen + 版本门 + 绑定符号。
  // 失败抛 Error:muse_unavailable(打不开 / 缺符号)、muse_version(版本不在窗口内)。
  static MuseLib load(const std::string& path);

  const Version& version() const noexcept { return version_; }
  const Api& api() const noexcept { return api_; }
  InitKind init_kind() const noexcept { return init_kind_; }

  // 全局初始化:样本根非空时先设置环境变量 MUSESAMPLER_INSTRUMENT_FOLDER,
  // 再调 ms_init_3 / ms_init_2,最后 ms_disable_reverb。失败抛 Error("muse_unavailable")。
  void init(const std::string& sample_root) const;

  // 枚举乐器目录(须在 init 之后)。
  std::vector<InstrumentEntry> list_instruments() const;

 private:
  MuseLib() = default;
  Api api_;
  Version version_;
  InitKind init_kind_ = InitKind::Init2;
};

}  // namespace cgmusic::muse
