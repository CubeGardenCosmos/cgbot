#pragma once

#include <array>
#include <cstdint>
#include <string>
#include <vector>

#include "common.hpp"

namespace cgmusic {

struct MidiEvent {
  double seconds = 0.0;      // 按速度表换算的绝对时间
  std::uint8_t status = 0;   // 0x80-0xEF,低 4 位为通道
  std::uint8_t data1 = 0;
  std::uint8_t data2 = 0;
  int channel = 0;           // 虚拟通道索引(可超出 0-15;未指定时取 status & 0x0F)

  MidiEvent() = default;
  MidiEvent(double sec, std::uint8_t st, std::uint8_t d1, std::uint8_t d2, int ch = -1)
      : seconds(sec), status(st), data1(d1), data2(d2),
        channel(ch >= 0 ? ch : static_cast<int>(st & 0x0F)) {}
};


// 速度段:从 tick 与 seconds 开始生效,直到下一段。
struct MidiTempo {
  std::uint64_t tick = 0;
  double seconds = 0.0;
  std::uint32_t microseconds_per_quarter = 500'000;  // 默认 120 BPM

  double bpm() const noexcept { return 60'000'000.0 / microseconds_per_quarter; }
};

// 拨奏/弓奏切换事件(仅 MusicXML 路径填充,MIDI 输入路径保持空)。
struct PizzicatoEv {
  double seconds = 0.0;
  bool pizz = false;  // true = 拨奏(pizz.)生效,false = 弓奏恢复(arco)
};

struct MidiSequence {
  std::vector<MidiEvent> events;     // 通道消息,按时间排序(同刻先 note-off)
  double duration_s = 0.0;           // 最长轨道结束点的时间
  std::vector<MidiTempo> tempo_map;  // 按时间排序;首项总在 0 秒(无速度事件时为 120 BPM)
  // 通道名称(索引即虚拟通道);从 MusicXML 声部名或 SMF 轨道名 0x03 元事件填充。
  std::vector<std::string> channel_names;
  // 通道打击乐标志(索引即虚拟通道);SMF 读取时通道 9 为 true。
  std::vector<bool> channel_is_drum;
  // 拨奏/弓奏切换时间列表(索引即虚拟通道);按 seconds 升序;仅 MusicXML 路径填充。
  std::vector<std::vector<PizzicatoEv>> pizzicato_ranges;
};

// 解析 SMF 0/1/2(metrical timing)。超过 15 分钟抛出 too_long。
MidiSequence parse_midi(const std::vector<std::uint8_t>& data);
double midi_duration(const std::vector<std::uint8_t>& data);

// 返回绝对时间 seconds 所处的速度段;早于首段或序列为空时返回首段/默认 120 BPM。
MidiTempo midi_tempo_at(const MidiSequence& sequence, double seconds);

}  // namespace cgmusic
