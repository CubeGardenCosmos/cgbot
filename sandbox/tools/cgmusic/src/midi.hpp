#pragma once

#include <cstdint>
#include <vector>

#include "common.hpp"

namespace cgmusic {

struct MidiEvent {
  double seconds = 0.0;      // 按速度表换算的绝对时间
  std::uint8_t status = 0;   // 0x80-0xEF,低 4 位为通道
  std::uint8_t data1 = 0;
  std::uint8_t data2 = 0;
};

struct MidiSequence {
  std::vector<MidiEvent> events;  // 通道消息,按时间排序(同刻先 note-off)
  double duration_s = 0.0;        // 最长轨道结束点的时间
};

// 解析 SMF 0/1/2(metrical timing)。超过 15 分钟抛出 too_long。
MidiSequence parse_midi(const std::vector<std::uint8_t>& data);
double midi_duration(const std::vector<std::uint8_t>& data);

}  // namespace cgmusic
