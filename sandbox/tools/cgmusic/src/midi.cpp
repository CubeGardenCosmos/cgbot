#include "midi.hpp"

#include <algorithm>
#include <cstdint>
#include <cstring>
#include <utility>

namespace cgmusic {
namespace {

constexpr std::size_t kMaxTracks = 256;
constexpr std::size_t kMaxEvents = 2'000'000;
constexpr std::uint32_t kDefaultMicrosPerQuarter = 500'000;  // 120 BPM

[[noreturn]] void bad_midi(const char* message) { throw Error("bad_input", message); }

std::uint32_t be32(const std::vector<std::uint8_t>& data, std::size_t at) {
  return (std::uint32_t{data[at]} << 24) | (std::uint32_t{data[at + 1]} << 16) |
         (std::uint32_t{data[at + 2]} << 8) | std::uint32_t{data[at + 3]};
}

std::uint16_t be16(const std::vector<std::uint8_t>& data, std::size_t at) {
  return static_cast<std::uint16_t>((std::uint16_t{data[at]} << 8) | data[at + 1]);
}

struct RawEvent {
  std::uint64_t tick = 0;
  std::uint8_t status = 0;
  std::uint8_t data1 = 0;
  std::uint8_t data2 = 0;
};

struct TempoChange {
  std::uint64_t tick = 0;
  std::uint32_t micros_per_quarter = 0;
};

bool is_note_on(const RawEvent& event) {
  return (event.status & 0xF0U) == 0x90U && event.data2 != 0;
}

// 有界的轨道字节读取器:任何越界都抛出 bad_input。
class TrackReader {
 public:
  TrackReader(const std::vector<std::uint8_t>& data, std::size_t begin, std::size_t end)
      : data_(data), pos_(begin), end_(end) {}

  bool done() const noexcept { return pos_ >= end_; }

  std::uint8_t peek() const {
    if (pos_ >= end_) bad_midi("Truncated MIDI track");
    return data_[pos_];
  }

  std::uint8_t next() {
    const std::uint8_t value = peek();
    ++pos_;
    return value;
  }

  std::uint32_t variable_length() {
    std::uint32_t value = 0;
    for (int i = 0; i < 4; ++i) {
      const std::uint8_t byte = next();
      value = (value << 7) | (byte & 0x7FU);
      if ((byte & 0x80U) == 0) return value;
    }
    bad_midi("Invalid MIDI variable-length quantity");
  }

  void skip(std::size_t count) {
    if (count > end_ - pos_) bad_midi("Truncated MIDI track");
    pos_ += count;
  }

 private:
  const std::vector<std::uint8_t>& data_;
  std::size_t pos_;
  std::size_t end_;
};

// 读取一条轨道:收集通道消息与速度变化;遇到 End of Track 结束。
// 支持 running status;sysex 取消 running status,meta 不影响。
void read_track(const std::vector<std::uint8_t>& data, std::size_t begin, std::size_t end,
                std::vector<RawEvent>& events, std::vector<TempoChange>& tempos,
                std::uint64_t& end_tick) {
  TrackReader reader(data, begin, end);
  std::uint64_t tick = 0;
  std::uint8_t running = 0;
  bool has_running = false;
  end_tick = 0;
  while (!reader.done()) {
    tick += reader.variable_length();
    std::uint8_t status = 0;
    if ((reader.peek() & 0x80U) != 0) {
      status = reader.next();
    } else {
      if (!has_running) bad_midi("MIDI running status without a status byte");
      status = running;
    }

    if (status == 0xFF) {
      const std::uint8_t type = reader.next();
      const std::uint32_t length = reader.variable_length();
      if (type == 0x51 && length == 3) {
        std::uint32_t micros = 0;
        for (int i = 0; i < 3; ++i) micros = (micros << 8) | reader.next();
        tempos.push_back({tick, micros});
      } else {
        reader.skip(length);
      }
      if (type == 0x2F) {
        end_tick = tick;
        return;
      }
    } else if (status == 0xF0 || status == 0xF7) {
      reader.skip(reader.variable_length());
      has_running = false;
    } else if (status >= 0x80 && status <= 0xEF) {
      running = status;
      has_running = true;
      const std::uint8_t kind = status & 0xF0U;
      RawEvent event;
      event.tick = tick;
      event.status = status;
      event.data1 = reader.next();
      if ((event.data1 & 0x80U) != 0) bad_midi("Invalid MIDI data byte");
      if (kind != 0xC0 && kind != 0xD0) {
        event.data2 = reader.next();
        if ((event.data2 & 0x80U) != 0) bad_midi("Invalid MIDI data byte");
      }
      events.push_back(event);
      if (events.size() > kMaxEvents) bad_midi("MIDI contains too many events");
    } else {
      bad_midi("Unsupported MIDI status byte");
    }
  }
  end_tick = tick;
}

// 速度表:每段记录起始刻、起始秒与每刻秒数,按刻查询绝对时间。
class TempoMap {
 public:
  TempoMap(std::uint16_t division, const std::vector<TempoChange>& changes) {
    const double seconds_per_quarter_unit = 1.0 / 1e6 / static_cast<double>(division);
    segments_.push_back({0, 0.0, kDefaultMicrosPerQuarter * seconds_per_quarter_unit, kDefaultMicrosPerQuarter});
    for (const auto& change : changes) {
      if (change.micros_per_quarter == 0) bad_midi("Invalid MIDI tempo");
      const double per_tick = change.micros_per_quarter * seconds_per_quarter_unit;
      Segment& last = segments_.back();
      if (change.tick == last.tick) {
        last.seconds_per_tick = per_tick;
        last.micros_per_quarter = change.micros_per_quarter;
        continue;
      }
      const double seconds =
          last.seconds + static_cast<double>(change.tick - last.tick) * last.seconds_per_tick;
      segments_.push_back({change.tick, seconds, per_tick, change.micros_per_quarter});
    }
  }

  double seconds_at(std::uint64_t tick) const {
    const auto it = std::upper_bound(
        segments_.begin(), segments_.end(), tick,
        [](std::uint64_t value, const Segment& segment) { return value < segment.tick; });
    const Segment& segment = *(it - 1);  // 首段从刻 0 开始,故总有前驱
    return segment.seconds + static_cast<double>(tick - segment.tick) * segment.seconds_per_tick;
  }

  // 导出速度表:每段的起始刻、起始秒与微秒/四分音符。
  std::vector<MidiTempo> tempos() const {
    std::vector<MidiTempo> out;
    out.reserve(segments_.size());
    for (const Segment& segment : segments_) {
      out.push_back({segment.tick, segment.seconds, segment.micros_per_quarter});
    }
    return out;
  }

 private:
  struct Segment {
    std::uint64_t tick;
    double seconds;
    double seconds_per_tick;
    std::uint32_t micros_per_quarter;
  };
  std::vector<Segment> segments_;
};

}  // namespace

MidiSequence parse_midi(const std::vector<std::uint8_t>& data) {
  if (data.size() > kMaxMidiBytes) throw Error("bad_input", "MIDI file exceeds size limit");
  if (data.size() < 14 || std::memcmp(data.data(), "MThd", 4) != 0) {
    bad_midi("Expected standard MIDI file");
  }
  const std::uint32_t header_length = be32(data, 4);
  if (header_length < 6 || header_length > data.size() - 8) bad_midi("Truncated MIDI header");
  const std::uint16_t format = be16(data, 8);
  const std::uint16_t declared_tracks = be16(data, 10);
  const std::uint16_t division = be16(data, 12);
  if (format > 2 || declared_tracks == 0 || declared_tracks > kMaxTracks) {
    bad_midi("Unsupported MIDI track layout");
  }
  if (division == 0 || (division & 0x8000U) != 0) {
    bad_midi("Only metrical MIDI timing is supported");
  }

  std::vector<RawEvent> raw;
  std::vector<TempoChange> tempos;
  std::uint64_t longest_tick = 0;
  std::size_t tracks = 0;
  std::size_t pos = 8 + static_cast<std::size_t>(header_length);
  while (tracks < declared_tracks && pos + 8 <= data.size()) {
    const std::size_t length = be32(data, pos + 4);
    const std::size_t body = pos + 8;
    if (length > data.size() - body) bad_midi("Truncated MIDI track chunk");
    if (std::memcmp(&data[pos], "MTrk", 4) == 0) {
      std::uint64_t end_tick = 0;
      read_track(data, body, body + length, raw, tempos, end_tick);
      longest_tick = std::max(longest_tick, end_tick);
      ++tracks;
    }
    pos = body + length;
  }
  if (tracks == 0) bad_midi("MIDI file has no tracks");

  std::stable_sort(tempos.begin(), tempos.end(),
                   [](const TempoChange& a, const TempoChange& b) { return a.tick < b.tick; });
  const TempoMap tempo_map(division, tempos);

  MidiSequence sequence;
  sequence.duration_s = tempo_map.seconds_at(longest_tick);
  sequence.tempo_map = tempo_map.tempos();
  if (sequence.duration_s > kMaxMidiSeconds) {
    throw Error("too_long", "The score exceeds the 15 minute playback limit");
  }

  std::stable_sort(raw.begin(), raw.end(), [](const RawEvent& a, const RawEvent& b) {
    if (a.tick != b.tick) return a.tick < b.tick;
    return !is_note_on(a) && is_note_on(b);  // 同刻先 note-off,避免刚开始的音被切断
  });
  sequence.events.reserve(raw.size());
  for (const auto& event : raw) {
    sequence.events.push_back(
        {tempo_map.seconds_at(event.tick), event.status, event.data1, event.data2});
  }
  return sequence;
}

double midi_duration(const std::vector<std::uint8_t>& data) {
  return parse_midi(data).duration_s;
}

MidiTempo midi_tempo_at(const MidiSequence& sequence, double seconds) {
  if (sequence.tempo_map.empty()) return MidiTempo{};
  const auto it = std::upper_bound(
      sequence.tempo_map.begin(), sequence.tempo_map.end(), seconds,
      [](double value, const MidiTempo& tempo) { return value < tempo.seconds; });
  if (it == sequence.tempo_map.begin()) return sequence.tempo_map.front();
  return *(it - 1);
}

}  // namespace cgmusic
