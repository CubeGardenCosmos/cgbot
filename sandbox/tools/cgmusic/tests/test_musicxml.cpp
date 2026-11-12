#include <cmath>
#include <cstdint>
#include <cstring>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

#include "common.hpp"
#include "midi.hpp"
#include "musicxml.hpp"
#include "test_harness.hpp"

namespace {

using Bytes = std::vector<std::uint8_t>;
using Entries = std::vector<std::pair<std::string, std::string>>;

// 轨道中的一个事件:通道消息为 status+数据;元事件为 FF 类型 负载(不含长度)
struct TrackEvent {
  std::uint64_t tick = 0;
  Bytes bytes;
};

Bytes to_bytes(std::string_view s) { return Bytes(s.begin(), s.end()); }

Bytes convert(std::string_view xml, std::string_view filename = "score.xml") {
  return cgmusic::musicxml_to_midi(to_bytes(xml), filename);
}

void expect_bad(const Bytes& data, std::string_view filename) {
  bool thrown = false;
  try {
    cgmusic::musicxml_to_midi(data, filename);
  } catch (const cgmusic::Error& e) {
    thrown = true;
    CGMUSIC_CHECK(e.code() == "bad_musicxml");
  }
  CGMUSIC_CHECK(thrown);
}

std::string partwise(std::string_view part_list, std::string_view parts) {
  std::string doc = "<?xml version=\"1.0\"?><score-partwise version=\"3.1\"><part-list>";
  doc += part_list;
  doc += "</part-list>";
  doc += parts;
  doc += "</score-partwise>";
  return doc;
}

std::string note(char step, int octave, int duration) {
  return std::string("<note><pitch><step>") + step + "</step><octave>" + std::to_string(octave) +
         "</octave></pitch><duration>" + std::to_string(duration) + "</duration><type>quarter</type></note>";
}

// 测试内实现的 CRC32(标准多项式 0xEDB88320)
std::uint32_t crc32(const Bytes& data) {
  std::uint32_t crc = 0xFFFFFFFFu;
  for (const std::uint8_t byte : data) {
    crc ^= byte;
    for (int bit = 0; bit < 8; ++bit) {
      crc = (crc & 1u) ? (crc >> 1) ^ 0xEDB88320u : crc >> 1;
    }
  }
  return ~crc;
}

void put16(Bytes& out, std::uint32_t v) {
  out.push_back(static_cast<std::uint8_t>(v & 0xFFu));
  out.push_back(static_cast<std::uint8_t>((v >> 8) & 0xFFu));
}

void put32(Bytes& out, std::uint32_t v) {
  put16(out, v & 0xFFFFu);
  put16(out, v >> 16);
}

void append_text(Bytes& out, std::string_view s) { out.insert(out.end(), s.begin(), s.end()); }

// 手写 STORED(method 0)zip:本地头 + 中央目录 + EOCD;日期固定为 1980-01-01
Bytes make_zip(const Entries& entries) {
  constexpr std::uint32_t kVersion = 20;
  constexpr std::uint32_t kDosDate = 0x21;
  struct Central {
    std::uint32_t crc;
    std::uint32_t size;
    std::uint32_t offset;
    const std::string* name;
  };
  Bytes out;
  std::vector<Central> central;
  for (const auto& [name, body] : entries) {
    const Bytes data = to_bytes(body);
    const auto crc = crc32(data);
    const auto size = static_cast<std::uint32_t>(data.size());
    const auto offset = static_cast<std::uint32_t>(out.size());
    put32(out, 0x04034b50u);  // 本地文件头签名
    put16(out, kVersion);
    put16(out, 0);  // flags
    put16(out, 0);  // method: STORED
    put16(out, 0);  // mod time
    put16(out, kDosDate);
    put32(out, crc);
    put32(out, size);
    put32(out, size);
    put16(out, static_cast<std::uint32_t>(name.size()));
    put16(out, 0);  // extra len
    append_text(out, name);
    out.insert(out.end(), data.begin(), data.end());
    central.push_back({crc, size, offset, &name});
  }
  const auto cd_offset = static_cast<std::uint32_t>(out.size());
  for (const auto& c : central) {
    put32(out, 0x02014b50u);  // 中央目录项签名
    put16(out, kVersion);     // made by
    put16(out, kVersion);     // needed
    put16(out, 0);            // flags
    put16(out, 0);            // method
    put16(out, 0);            // mod time
    put16(out, kDosDate);
    put32(out, c.crc);
    put32(out, c.size);
    put32(out, c.size);
    put16(out, static_cast<std::uint32_t>(c.name->size()));
    put16(out, 0);  // extra len
    put16(out, 0);  // comment len
    put16(out, 0);  // disk start
    put16(out, 0);  // internal attr
    put32(out, 0);  // external attr
    put32(out, c.offset);
    append_text(out, *c.name);
  }
  const auto cd_size = static_cast<std::uint32_t>(out.size()) - cd_offset;
  put32(out, 0x06054b50u);  // EOCD 签名
  put16(out, 0);            // disk
  put16(out, 0);            // disk with cd
  put16(out, static_cast<std::uint32_t>(entries.size()));
  put16(out, static_cast<std::uint32_t>(entries.size()));
  put32(out, cd_size);
  put32(out, cd_offset);
  put16(out, 0);  // comment len
  return out;
}

std::uint32_t be32(const Bytes& b, std::size_t at) {
  return (static_cast<std::uint32_t>(b[at]) << 24) | (static_cast<std::uint32_t>(b[at + 1]) << 16) |
         (static_cast<std::uint32_t>(b[at + 2]) << 8) | static_cast<std::uint32_t>(b[at + 3]);
}

std::uint64_t read_vlq(const Bytes& b, std::size_t& pos) {
  std::uint64_t value = 0;
  for (int i = 0; i < 4; ++i) {
    CGMUSIC_CHECK(pos < b.size());
    const std::uint8_t c = b[pos++];
    value = (value << 7) | (c & 0x7F);
    if ((c & 0x80) == 0) return value;
  }
  CGMUSIC_CHECK(false);
  return value;
}

std::vector<TrackEvent> parse_track(const Bytes& m, std::size_t pos, std::size_t end) {
  std::vector<TrackEvent> out;
  std::uint64_t tick = 0;
  while (pos < end) {
    tick += read_vlq(m, pos);
    CGMUSIC_CHECK(pos < end);
    TrackEvent ev;
    ev.tick = tick;
    if (m[pos] == 0xFF) {
      CGMUSIC_CHECK(pos + 2 <= end);
      const std::uint8_t kind = m[pos + 1];
      std::size_t p = pos + 2;
      const std::uint64_t len = read_vlq(m, p);
      CGMUSIC_CHECK(p + len <= end);
      ev.bytes.push_back(0xFF);
      ev.bytes.push_back(kind);
      ev.bytes.insert(ev.bytes.end(), m.begin() + static_cast<std::ptrdiff_t>(p),
                      m.begin() + static_cast<std::ptrdiff_t>(p + len));
      pos = p + len;
    } else {
      // 不使用 running status;通道消息必须带 status
      CGMUSIC_CHECK((m[pos] & 0x80) != 0 && m[pos] < 0xF0);
      const std::size_t n = (m[pos] & 0xE0) == 0xC0 ? 2 : 3;
      CGMUSIC_CHECK(pos + n <= end);
      ev.bytes.assign(m.begin() + static_cast<std::ptrdiff_t>(pos),
                      m.begin() + static_cast<std::ptrdiff_t>(pos + n));
      pos += n;
    }
    out.push_back(std::move(ev));
  }
  return out;
}

std::vector<TrackEvent> track_events(const Bytes& midi, std::size_t index) {
  CGMUSIC_CHECK(midi.size() >= 14 && std::memcmp(midi.data(), "MThd", 4) == 0);
  std::size_t pos = 8 + be32(midi, 4);
  for (std::size_t t = 0;; ++t) {
    CGMUSIC_CHECK(pos + 8 <= midi.size());
    CGMUSIC_CHECK(std::memcmp(midi.data() + pos, "MTrk", 4) == 0);
    const std::size_t len = be32(midi, pos + 4);
    CGMUSIC_CHECK(pos + 8 + len <= midi.size());
    if (t == index) return parse_track(midi, pos + 8, pos + 8 + len);
    pos += 8 + len;
  }
}

std::size_t index_of(const std::vector<TrackEvent>& events, std::uint64_t tick, const Bytes& bytes) {
  for (std::size_t i = 0; i < events.size(); ++i) {
    if (events[i].tick == tick && events[i].bytes == bytes) return i;
  }
  return events.size();
}

bool has_event(const std::vector<TrackEvent>& events, std::uint64_t tick, const Bytes& bytes) {
  return index_of(events, tick, bytes) < events.size();
}

// 音符开始(note-on 且力度非零)的 tick
std::vector<std::uint64_t> note_on_ticks(const std::vector<TrackEvent>& events) {
  std::vector<std::uint64_t> ticks;
  for (const TrackEvent& e : events) {
    if (e.bytes.size() == 3 && (e.bytes[0] & 0xF0) == 0x90 && e.bytes[2] != 0) ticks.push_back(e.tick);
  }
  return ticks;
}

std::vector<std::uint8_t> note_on_pitches(const std::vector<TrackEvent>& events) {
  std::vector<std::uint8_t> pitches;
  for (const TrackEvent& e : events) {
    if (e.bytes.size() == 3 && (e.bytes[0] & 0xF0) == 0x90 && e.bytes[2] != 0) pitches.push_back(e.bytes[1]);
  }
  return pitches;
}

bool near(double a, double b) { return std::fabs(a - b) < 1e-6; }

}  // namespace

CGMUSIC_TEST(musicxml_basic_trumpet_score) {
  const std::string xml = partwise(
      "<score-part id=\"P1\"><part-name>Trumpet</part-name></score-part>",
      "<part id=\"P1\"><measure number=\"1\">"
      "<attributes><divisions>2</divisions><key><fifths>2</fifths></key>"
      "<time><beats>3</beats><beat-type>4</beat-type></time></attributes>"
      "<direction><direction-type><dynamics><f/></dynamics></direction-type>"
      "<sound tempo=\"90\"/></direction>"
      "<note><pitch><step>C</step><alter>1</alter><octave>4</octave></pitch>"
      "<duration>2</duration><type>quarter</type></note>"
      "</measure></part>");
  const Bytes midi = convert(xml);
  const std::vector<TrackEvent> events = track_events(midi, 0);
  const Bytes program = {0xC0, 56};
  const Bytes tempo = {0xFF, 0x51, 0x0A, 0x2C, 0x2B};
  const Bytes time_sig = {0xFF, 0x58, 0x03, 0x02, 0x18, 0x08};
  const Bytes key_sig = {0xFF, 0x59, 0x02, 0x00};
  const Bytes on = {0x90, 61, 96};
  const Bytes off = {0x80, 61, 0};
  CGMUSIC_CHECK(has_event(events, 0, program));
  CGMUSIC_CHECK(has_event(events, 0, tempo));
  CGMUSIC_CHECK(has_event(events, 0, time_sig));
  CGMUSIC_CHECK(has_event(events, 0, key_sig));
  CGMUSIC_CHECK(has_event(events, 0, on));
  CGMUSIC_CHECK(has_event(events, 480, off));
  CGMUSIC_CHECK(index_of(events, 0, program) < index_of(events, 0, on));
  const std::vector<std::uint64_t> expected_on = {0};
  CGMUSIC_CHECK(note_on_ticks(events) == expected_on);
  CGMUSIC_CHECK(near(cgmusic::midi_duration(midi), 2.0 / 3.0));
}

CGMUSIC_TEST(musicxml_chord_rest_backup_forward_tie) {
  const std::string xml = partwise(
      "<score-part id=\"P1\"><part-name>Piano</part-name></score-part>",
      "<part id=\"P1\">"
      "<measure number=\"1\"><attributes><divisions>1</divisions></attributes>"
      "<note><pitch><step>C</step><octave>4</octave></pitch><duration>2</duration>"
      "<tie type=\"start\"/><type>half</type></note>"
      "<note><chord/><pitch><step>E</step><octave>4</octave></pitch><duration>2</duration><type>half</type></note>"
      "<backup><duration>2</duration></backup>"
      "<forward><duration>1</duration></forward>"
      "<note><rest/><duration>1</duration><type>quarter</type></note>"
      "</measure>"
      "<measure number=\"2\">"
      "<note><pitch><step>C</step><octave>4</octave></pitch><duration>1</duration>"
      "<tie type=\"stop\"/><type>quarter</type></note>"
      "</measure></part>");
  const std::vector<TrackEvent> events = track_events(convert(xml), 0);
  CGMUSIC_CHECK(has_event(events, 0, Bytes{0x90, 60, 80}));
  CGMUSIC_CHECK(has_event(events, 0, Bytes{0x90, 64, 80}));
  CGMUSIC_CHECK(has_event(events, 1440, Bytes{0x80, 60, 0}));
  CGMUSIC_CHECK(has_event(events, 960, Bytes{0x80, 64, 0}));
  CGMUSIC_CHECK(note_on_ticks(events).size() == 2);
}

CGMUSIC_TEST(musicxml_grace_transpose_program) {
  const std::string xml = partwise(
      "<score-part id=\"P1\"><part-name>Flute</part-name></score-part>",
      "<part id=\"P1\"><measure number=\"1\">"
      "<attributes><divisions>1</divisions>"
      "<transpose><chromatic>12</chromatic></transpose></attributes>"
      "<note><grace/><pitch><step>C</step><octave>4</octave></pitch><type>eighth</type></note>"
      "<note><pitch><step>D</step><octave>4</octave></pitch><duration>1</duration><type>quarter</type></note>"
      "</measure></part>");
  const std::vector<TrackEvent> events = track_events(convert(xml), 0);
  CGMUSIC_CHECK(has_event(events, 0, Bytes{0xC0, 73}));
  CGMUSIC_CHECK(has_event(events, 0, Bytes{0x90, 72, 80}));
  CGMUSIC_CHECK(has_event(events, 0, Bytes{0x90, 74, 80}));
  CGMUSIC_CHECK(has_event(events, 30, Bytes{0x80, 72, 0}));
}

CGMUSIC_TEST(musicxml_transposing_clarinet) {
  const std::string xml = partwise(
      "<score-part id=\"P1\"><part-name>Clarinet</part-name></score-part>",
      "<part id=\"P1\"><measure number=\"1\">"
      "<attributes><divisions>1</divisions>"
      "<transpose><chromatic>-2</chromatic></transpose></attributes>"
      "<note><pitch><step>C</step><octave>4</octave></pitch><duration>1</duration><type>quarter</type></note>"
      "</measure></part>");
  const std::vector<TrackEvent> events = track_events(convert(xml), 0);
  CGMUSIC_CHECK(has_event(events, 0, Bytes{0xC0, 71}));
  CGMUSIC_CHECK(has_event(events, 0, Bytes{0x90, 58, 80}));
  CGMUSIC_CHECK(has_event(events, 480, Bytes{0x80, 58, 0}));
}

CGMUSIC_TEST(musicxml_drum_unpitched_mapping) {
  const std::string xml = partwise(
      "<score-part id=\"P1\"><part-name>Drums</part-name>"
      "<score-instrument id=\"I1\"><instrument-name>Snare</instrument-name></score-instrument></score-part>",
      "<part id=\"P1\"><measure number=\"1\">"
      "<attributes><divisions>1</divisions></attributes>"
      "<note><unpitched><display-step>C</display-step><display-octave>4</display-octave></unpitched>"
      "<duration>1</duration><instrument id=\"I1\"/><type>quarter</type></note>"
      "</measure></part>");
  const std::vector<TrackEvent> events = track_events(convert(xml), 0);
  CGMUSIC_CHECK(has_event(events, 0, Bytes{0x99, 38, 80}));
  for (const TrackEvent& e : events) {
    if (!e.bytes.empty() && e.bytes[0] >= 0x80 && e.bytes[0] < 0xF0) CGMUSIC_CHECK((e.bytes[0] & 0x0F) == 9);
  }
}

CGMUSIC_TEST(musicxml_simple_repeat) {
  const std::string xml = partwise(
      "<score-part id=\"P1\"><part-name>Piano</part-name></score-part>",
      "<part id=\"P1\">"
      "<measure number=\"1\"><barline location=\"left\"><repeat direction=\"forward\"/></barline>" +
          note('C', 4, 1) +
          "</measure>"
          "<measure number=\"2\">" +
          note('D', 4, 1) +
          "<barline location=\"right\"><repeat direction=\"backward\" times=\"2\"/></barline>"
          "</measure></part>");
  const Bytes midi = convert(xml);
  const std::vector<TrackEvent> events = track_events(midi, 0);
  const std::vector<std::uint64_t> starts = {0, 480, 960, 1440};
  const std::vector<std::uint8_t> pitches = {60, 62, 60, 62};
  CGMUSIC_CHECK(note_on_ticks(events) == starts);
  CGMUSIC_CHECK(note_on_pitches(events) == pitches);
  CGMUSIC_CHECK(near(cgmusic::midi_duration(midi), 2.0));
}

CGMUSIC_TEST(musicxml_volta_endings) {
  const std::string xml = partwise(
      "<score-part id=\"P1\"><part-name>Piano</part-name></score-part>",
      "<part id=\"P1\">"
      "<measure number=\"1\"><barline location=\"left\"><repeat direction=\"forward\"/></barline>" +
          note('C', 4, 1) +
          "</measure>"
          "<measure number=\"2\">" +
          note('D', 4, 1) +
          "</measure>"
          "<measure number=\"3\"><barline location=\"left\"><ending number=\"1\" type=\"start\"/></barline>" +
          note('E', 4, 1) +
          "<barline location=\"right\"><ending number=\"1\" type=\"stop\"/>"
          "<repeat direction=\"backward\" times=\"2\"/></barline>"
          "</measure>"
          "<measure number=\"4\"><barline location=\"left\"><ending number=\"2\" type=\"start\"/></barline>" +
          note('F', 4, 1) +
          "<barline location=\"right\"><ending number=\"2\" type=\"stop\"/></barline>"
          "</measure></part>");
  const Bytes midi = convert(xml);
  const std::vector<TrackEvent> events = track_events(midi, 0);
  const std::vector<std::uint64_t> starts = {0, 480, 960, 1440, 1920, 2400};
  const std::vector<std::uint8_t> pitches = {60, 62, 64, 60, 62, 65};
  CGMUSIC_CHECK(note_on_ticks(events) == starts);
  CGMUSIC_CHECK(note_on_pitches(events) == pitches);
  CGMUSIC_CHECK(near(cgmusic::midi_duration(midi), 3.0));
}

CGMUSIC_TEST(musicxml_direction_offset_tempo) {
  const std::string xml = partwise(
      "<score-part id=\"P1\"><part-name>Piano</part-name></score-part>",
      "<part id=\"P1\"><measure number=\"1\">"
      "<attributes><divisions>1</divisions></attributes>"
      "<direction><direction-type><metronome><beat-unit>quarter</beat-unit>"
      "<per-minute>60</per-minute></metronome></direction-type><offset>1</offset></direction>" +
          note('C', 4, 1) + note('D', 4, 1) +
          "</measure></part>");
  const Bytes midi = convert(xml);
  const std::vector<TrackEvent> events = track_events(midi, 0);
  CGMUSIC_CHECK(has_event(events, 480, Bytes{0xFF, 0x51, 0x0F, 0x42, 0x40}));
  CGMUSIC_CHECK(near(cgmusic::midi_duration(midi), 1.5));
}

CGMUSIC_TEST(musicxml_channel_allocation_skips_declared) {
  const std::string xml = partwise(
      "<score-part id=\"P1\"><part-name>Piano</part-name></score-part>",
      "<part id=\"X\"><measure number=\"1\">" + note('C', 4, 1) + "</measure></part>"
      "<part id=\"P1\"><measure number=\"1\">" + note('E', 4, 1) + "</measure></part>");
  const Bytes midi = convert(xml);
  CGMUSIC_CHECK(has_event(track_events(midi, 0), 0, Bytes{0x91, 60, 80}));
  CGMUSIC_CHECK(has_event(track_events(midi, 1), 0, Bytes{0x90, 64, 80}));
}

CGMUSIC_TEST(musicxml_program_names_longest_match) {
  const std::string xml = partwise(
      "<score-part id=\"P1\"><part-name>English Horn</part-name></score-part>"
      "<score-part id=\"P2\"><part-name>Contrabassoon</part-name></score-part>"
      "<score-part id=\"P3\"><part-name>Contrabass</part-name></score-part>"
      "<score-part id=\"P4\"><part-name>Bassoon</part-name></score-part>",
      "<part id=\"P1\"><measure number=\"1\"/></part>"
      "<part id=\"P2\"><measure number=\"1\"/></part>"
      "<part id=\"P3\"><measure number=\"1\"/></part>"
      "<part id=\"P4\"><measure number=\"1\"/></part>");
  const Bytes midi = convert(xml);
  CGMUSIC_CHECK(has_event(track_events(midi, 0), 0, Bytes{0xC0, 69}));
  CGMUSIC_CHECK(has_event(track_events(midi, 1), 0, Bytes{0xC1, 70}));
  CGMUSIC_CHECK(has_event(track_events(midi, 2), 0, Bytes{0xC2, 43}));
  CGMUSIC_CHECK(has_event(track_events(midi, 3), 0, Bytes{0xC3, 70}));
}

CGMUSIC_TEST(musicxml_mxl_timewise) {
  const Bytes archive = make_zip({
      {"META-INF/container.xml",
       "<?xml version=\"1.0\"?><container><rootfiles>"
       "<rootfile full-path=\"score.xml\"/></rootfiles></container>"},
      {"score.xml",
       "<?xml version=\"1.0\"?><score-timewise version=\"3.1\"><part-list>"
       "<score-part id=\"P1\"><part-name>Piano</part-name></score-part></part-list>"
       "<measure number=\"1\"><part id=\"P1\"><attributes><divisions>1</divisions></attributes>"
       "<note><pitch><step>G</step><octave>4</octave></pitch><duration>1</duration></note>"
       "</part></measure></score-timewise>"},
  });
  const std::vector<TrackEvent> parsed = track_events(cgmusic::musicxml_to_midi(archive, "score.mxl"), 0);
  CGMUSIC_CHECK(has_event(parsed, 0, Bytes{0x90, 67, 80}));
}

CGMUSIC_TEST(musicxml_rejects_bad_inputs) {
  expect_bad(Bytes{}, "score.xml");
  expect_bad(to_bytes("not xml"), "score.xml");
  expect_bad(to_bytes("<html/>"), "score.xml");
  expect_bad(to_bytes("PKbad"), "score.xml");
}

CGMUSIC_TEST(musicxml_mxl_rejects_parent_path) {
  const Bytes archive = make_zip({
      {"META-INF/container.xml",
       "<?xml version=\"1.0\"?><container><rootfiles>"
       "<rootfile full-path=\"../secret\"/></rootfiles></container>"},
      {"score.xml", "<score-partwise/>"},
  });
  expect_bad(archive, "score.mxl");
}

CGMUSIC_TEST(musicxml_mxl_unsafe_path_falls_back) {
  const Bytes archive = make_zip({
      {"META-INF/container.xml",
       "<?xml version=\"1.0\"?><container><rootfiles>"
       "<rootfile full-path=\"../score.xml\"/></rootfiles></container>"},
      {"score.xml", partwise("<score-part id=\"P1\"><part-name>Piano</part-name></score-part>",
                             "<part id=\"P1\"><measure number=\"1\">" + note('G', 4, 1) + "</measure></part>")},
  });
  const std::vector<TrackEvent> events = track_events(cgmusic::musicxml_to_midi(archive, "score.mxl"), 0);
  CGMUSIC_CHECK(has_event(events, 0, Bytes{0x90, 67, 80}));
}

CGMUSIC_TEST(musicxml_synth_compatible_duration) {
  const std::string xml = partwise(
      "<score-part id=\"P1\"><part-name>Piano</part-name></score-part>",
      "<part id=\"P1\"><measure number=\"1\">" + note('C', 4, 1) + "</measure></part>");
  CGMUSIC_CHECK(near(cgmusic::midi_duration(convert(xml)), 0.5));
}

CGMUSIC_TEST(musicxml_rejects_duplicate_part_id) {
  const std::string xml = partwise(
      "<score-part id=\"P1\"><part-name>Piano</part-name></score-part>",
      "<part id=\"P1\"><measure number=\"1\"/></part>"
      "<part id=\"P1\"><measure number=\"1\"/></part>");
  expect_bad(to_bytes(xml), "score.xml");
}

CGMUSIC_TEST(musicxml_rejects_nonpositive_tempo) {
  const std::string xml = partwise(
      "<score-part id=\"P1\"><part-name>Piano</part-name></score-part>",
      "<part id=\"P1\"><measure number=\"1\">"
      "<direction><direction-type><dynamics><f/></dynamics></direction-type>"
      "<sound tempo=\"0\"/></direction>" +
          note('C', 4, 1) + "</measure></part>");
  expect_bad(to_bytes(xml), "score.xml");
}
