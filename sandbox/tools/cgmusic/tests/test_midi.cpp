#include <cmath>
#include <cstdint>
#include <string>
#include <utility>
#include <vector>

#include "common.hpp"
#include "midi.hpp"
#include "test_harness.hpp"

namespace {

using Bytes = std::vector<std::uint8_t>;
using Event = std::pair<std::uint32_t, Bytes>;  // (delta, payload)

Bytes vlq(std::uint32_t value) {
  Bytes out{static_cast<std::uint8_t>(value & 0x7FU)};
  value >>= 7;
  while (value != 0) {
    out.insert(out.begin(), static_cast<std::uint8_t>(0x80U | (value & 0x7FU)));
    value >>= 7;
  }
  return out;
}

void append_be32(Bytes& out, std::uint32_t value) {
  for (int shift = 24; shift >= 0; shift -= 8) {
    out.push_back(static_cast<std::uint8_t>(value >> shift));
  }
}

// 构造单轨 SMF;调用方负责给出 End of Track。
Bytes smf(std::uint16_t format, std::uint16_t division, const std::vector<Event>& events) {
  Bytes track;
  for (const auto& [delta, payload] : events) {
    const Bytes encoded = vlq(delta);
    track.insert(track.end(), encoded.begin(), encoded.end());
    track.insert(track.end(), payload.begin(), payload.end());
  }
  Bytes out{'M', 'T', 'h', 'd', 0, 0, 0, 6, 0, static_cast<std::uint8_t>(format),
            0, 1, static_cast<std::uint8_t>(division >> 8), static_cast<std::uint8_t>(division),
            'M', 'T', 'r', 'k'};
  append_be32(out, static_cast<std::uint32_t>(track.size()));
  out.insert(out.end(), track.begin(), track.end());
  return out;
}

const Bytes kEndOfTrack{0xFF, 0x2F, 0x00};

bool near(double a, double b) { return std::fabs(a - b) < 1e-9; }

std::string error_code_of(const std::vector<std::uint8_t>& data) {
  try {
    cgmusic::parse_midi(data);
  } catch (const cgmusic::Error& error) {
    return error.code();
  }
  return "none";
}

}  // namespace

// 与 cgbot-music-wip/tests/test_synth.py 的 _midi 固定样例一致:format 0,480 PPQ,C4 一拍。
CGMUSIC_TEST(golden_single_note_duration_and_events) {
  const Bytes data = smf(0, 480,
                         {{0, {0x90, 0x3C, 0x60}}, {480, {0x80, 0x3C, 0x00}}, {0, kEndOfTrack}});
  const cgmusic::MidiSequence sequence = cgmusic::parse_midi(data);
  CGMUSIC_CHECK(sequence.events.size() == 2);
  CGMUSIC_CHECK(near(sequence.duration_s, 0.5));
  CGMUSIC_CHECK(near(cgmusic::midi_duration(data), 0.5));
  CGMUSIC_CHECK(near(sequence.events[0].seconds, 0.0));
  CGMUSIC_CHECK(sequence.events[0].status == 0x90);
  CGMUSIC_CHECK(sequence.events[0].data1 == 0x3C);
  CGMUSIC_CHECK(near(sequence.events[1].seconds, 0.5));
}

CGMUSIC_TEST(tempo_map_changes_seconds_per_tick) {
  // 0 刻 500000 us/拍(0.5 s/480 刻);480 刻起改为 1000000 us/拍。
  // 音符在 480 刻开始,960 刻结束:0.5 s + 480 刻 * 1 s/480 = 1.5 s。
  const Bytes data = smf(0, 480,
                         {{0, {0xFF, 0x51, 0x03, 0x07, 0xA1, 0x20}},
                          {480, {0xFF, 0x51, 0x03, 0x0F, 0x42, 0x40}},
                          {0, {0x90, 0x3C, 0x60}},
                          {480, {0x80, 0x3C, 0x00}},
                          {0, kEndOfTrack}});
  const cgmusic::MidiSequence sequence = cgmusic::parse_midi(data);
  CGMUSIC_CHECK(near(sequence.duration_s, 1.5));
  CGMUSIC_CHECK(sequence.events.size() == 2);
  CGMUSIC_CHECK(near(sequence.events[0].seconds, 0.5));
  CGMUSIC_CHECK(near(sequence.events[1].seconds, 1.5));
}

CGMUSIC_TEST(running_status_is_expanded) {
  // 第二个 note-on 省略状态字节,应沿用 0x90。
  const Bytes data = smf(0, 480,
                         {{0, {0x90, 0x3C, 0x60}}, {0, {0x3E, 0x60}}, {480, {0x80, 0x3C, 0x00}},
                          {0, {0x80, 0x3E, 0x00}}, {0, kEndOfTrack}});
  const cgmusic::MidiSequence sequence = cgmusic::parse_midi(data);
  CGMUSIC_CHECK(sequence.events.size() == 4);
  CGMUSIC_CHECK(sequence.events[1].status == 0x90);
  CGMUSIC_CHECK(sequence.events[1].data1 == 0x3E);
  CGMUSIC_CHECK(sequence.events[1].data2 == 0x60);
}

CGMUSIC_TEST(sixteen_minutes_is_rejected_as_too_long) {
  // 默认速度下 480 刻 = 0.5 s,921600 刻 = 960 s > 15 分钟。
  const Bytes data = smf(0, 480,
                         {{0, {0x90, 0x3C, 0x60}}, {921600, {0x80, 0x3C, 0x00}},
                          {0, kEndOfTrack}});
  CGMUSIC_CHECK(error_code_of(data) == "too_long");
}

CGMUSIC_TEST(bad_header_and_truncated_track_are_bad_input) {
  const Bytes not_midi{'R', 'I', 'F', 'F', 0, 0, 0, 6, 0, 0, 0, 1, 0, 0};
  CGMUSIC_CHECK(error_code_of(not_midi) == "bad_input");

  Bytes truncated = smf(0, 480, {{0, {0x90, 0x3C, 0x60}}, {0, kEndOfTrack}});
  truncated.resize(truncated.size() - 2);
  CGMUSIC_CHECK(error_code_of(truncated) == "bad_input");
}
