#pragma once

// MuseSampler 核心库(libMuseSamplerCoreLib)的最小 C ABI 声明。
//
// 只声明 cgmusic 实际调用的子集,按公开的函数名与结构体布局自行书写,
// 不是任何第三方头文件的拷贝。库本身不可再分发,运行期用 dlopen 取符号(见 loader.hpp)。
//
// 验证状态见 tools/muse_probe.cpp 与任务报告:
// - 结构体大小与偏移由下方 static_assert 固定;尚未用真实库实机核对(见 docs/reference/musesampler.md §15.2)。
// - ms_init_3 无参数(反汇编:入口不读取任何参数寄存器),返回 ms_Result。

#include <cstddef>
#include <cstdint>

namespace cgmusic::muse::abi {

using ms_Result = int;
inline constexpr ms_Result kResultOk = 0;
inline constexpr ms_Result kResultError = -1;
inline constexpr ms_Result kResultTimeout = -2;

// 不透明句柄。
using ms_MuseSampler = void*;
using ms_Track = void*;
using ms_InstrumentList = void*;
using ms_InstrumentInfo = void*;

// 离线渲染输出缓冲:_channels 指向 _num_channels 个各含 _num_data_pts 个 float 的数组。
// 按值传递;16 字节,SysV 下占两个整数寄存器。
struct ms_OutputBuffer {
  float** _channels;
  int _num_data_pts;
  int _num_channels;
};

// 力度事件(0.4 起)。_value 取 0.0..1.0。按值传递:{INTEGER, SSE}。
struct ms_DynamicsEvent_2 {
  long long _location_us;
  double _value;
};

// 音符关节标志(ms_NoteEvent_5::_articulation),只列出本项目用到的位。
enum : std::uint64_t {
  kArticulationNone = 0,
  kArticulationStaccato = 1ULL << 0,
  kArticulationAccent = 1ULL << 2,
  kArticulationTenuto = 1ULL << 3,
  kArticulationMarcato = 1ULL << 4,
  kArticulationPizzicato = 1ULL << 37,
};

// 音符事件(0.102 起)。72 字节,按值传递时走栈(MEMORY 类)。
struct ms_NoteEvent_5 {
  int _voice;                       // 0..3
  long long _location_us;           // 起点,微秒
  long long _duration_us;           // 时值,微秒
  int _pitch;                       // MIDI 音高
  double _tempo;                    // BPM
  int _offset_cents;                // 音分偏移
  std::uint64_t _articulation;      // ms_NoteArticulation 位集
  std::uint64_t _articulation_2;    // ms_NoteArticulation2 位集
  std::int16_t _notehead;           // ms_NoteHead,0 = Normal
};

static_assert(sizeof(ms_OutputBuffer) == 16);
static_assert(offsetof(ms_OutputBuffer, _num_data_pts) == 8);
static_assert(offsetof(ms_OutputBuffer, _num_channels) == 12);
static_assert(sizeof(ms_DynamicsEvent_2) == 16);
static_assert(offsetof(ms_DynamicsEvent_2, _value) == 8);
static_assert(sizeof(ms_NoteEvent_5) == 72);
static_assert(offsetof(ms_NoteEvent_5, _location_us) == 8);
static_assert(offsetof(ms_NoteEvent_5, _duration_us) == 16);
static_assert(offsetof(ms_NoteEvent_5, _pitch) == 24);
static_assert(offsetof(ms_NoteEvent_5, _tempo) == 32);
static_assert(offsetof(ms_NoteEvent_5, _offset_cents) == 40);
static_assert(offsetof(ms_NoteEvent_5, _articulation) == 48);
static_assert(offsetof(ms_NoteEvent_5, _articulation_2) == 56);
static_assert(offsetof(ms_NoteEvent_5, _notehead) == 64);

// 函数指针类型:名称 = 导出符号名 + "_fn"。
extern "C" {
// 版本
using ms_get_version_string_fn = const char* (*)();
using ms_get_version_major_fn = int (*)();
using ms_get_version_minor_fn = int (*)();
using ms_get_version_revision_fn = int (*)();
using ms_get_version_build_number_fn = int (*)();

// 全局初始化(无参数)。0.106+ 用 ms_init_3,更低版本用 ms_init_2。
using ms_init_fn = ms_Result (*)();
using ms_init_2_fn = ms_Result (*)();
using ms_init_3_fn = ms_Result (*)();
using ms_disable_reverb_fn = ms_Result (*)();

// 乐器目录
using ms_get_instrument_list_fn = ms_InstrumentList (*)();
using ms_InstrumentList_get_next_fn = ms_InstrumentInfo (*)(ms_InstrumentList);
using ms_Instrument_get_id_fn = int (*)(ms_InstrumentInfo);
using ms_Instrument_get_name_fn = const char* (*)(ms_InstrumentInfo);
using ms_Instrument_get_category_fn = const char* (*)(ms_InstrumentInfo);
using ms_Instrument_get_package_fn = const char* (*)(ms_InstrumentInfo);
using ms_Instrument_get_pack_name_fn = const char* (*)(ms_InstrumentInfo);
using ms_Instrument_get_vendor_name_fn = const char* (*)(ms_InstrumentInfo);
using ms_Instrument_get_musicxml_sound_fn = const char* (*)(ms_InstrumentInfo);
using ms_Instrument_get_mpe_sound_fn = const char* (*)(ms_InstrumentInfo);
using ms_Instrument_get_reverb_level_fn = float (*)(ms_InstrumentInfo);

// 采样器实例与轨道
using ms_MuseSampler_create_fn = ms_MuseSampler (*)();
using ms_MuseSampler_destroy_fn = void (*)(ms_MuseSampler);
using ms_MuseSampler_init_2_fn = ms_Result (*)(ms_MuseSampler, double sample_rate, int block_size,
                                               int channel_count);
using ms_MuseSampler_add_track_fn = ms_Track (*)(ms_MuseSampler, int instrument_id);
using ms_MuseSampler_finalize_track_fn = ms_Result (*)(ms_MuseSampler, ms_Track);
using ms_MuseSampler_clear_score_fn = ms_Result (*)(ms_MuseSampler);

// 事件。event_id 是输出参数(C++ 里的 long long&,ABI 上就是指针)。
using ms_MuseSampler_add_track_note_event_6_fn = ms_Result (*)(ms_MuseSampler, ms_Track,
                                                               ms_NoteEvent_5, long long* event_id);
using ms_MuseSampler_add_track_dynamics_event_2_fn = ms_Result (*)(ms_MuseSampler, ms_Track,
                                                                   ms_DynamicsEvent_2);

// 渲染状态(0.101 / 0.102 起)
using ms_MuseSampler_ready_to_play_fn = bool (*)(ms_MuseSampler);
using ms_MuseSampler_trigger_render_fn = void (*)(ms_MuseSampler);

// 离线渲染
using ms_MuseSampler_start_offline_mode_fn = ms_Result (*)(ms_MuseSampler, double sample_rate);
using ms_MuseSampler_stop_offline_mode_fn = ms_Result (*)(ms_MuseSampler);
using ms_MuseSampler_process_offline_fn = ms_Result (*)(ms_MuseSampler, ms_OutputBuffer);
}

}  // namespace cgmusic::muse::abi
