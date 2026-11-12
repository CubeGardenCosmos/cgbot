#include "balance.hpp"

namespace cgmusic {
namespace {

constexpr int kDrumChannel = 9;  // MIDI 通道 10

struct ProgramRule {
  int first;
  int last;
  Stem stem;
};

// GM 程序号(0 基,与 MIDI 程序号一致)到声部。
constexpr ProgramRule kProgramRules[] = {
    {21, 21, Stem::Accordion},  // Accordion
    {23, 23, Stem::Accordion},  // Tango Accordion(22 Harmonica 保持在 Melodic)
    {56, 63, Stem::Brass},      // Trumpet 至 Synth Brass 2
    {64, 79, Stem::Woodwind},   // Soprano Sax 至 Ocarina
};

struct NoteRule {
  int first;
  int last;
  Stem stem;
};

// 鼓组音符到声部。这些声部的偏移是绝对值,不与鼓组整体偏移叠加;未列出的音符归入 OtherDrum。
constexpr NoteRule kDrumRules[] = {
    {35, 36, Stem::Kick},
    {38, 40, Stem::Snare},
};

struct StemLevel {
  const char* name;
  double offset_db;
};

// 下标与 Stem 枚举一致。
constexpr StemLevel kStemLevels[] = {
    {"melodic", 0.0},
    {"woodwind", -6.0},
    {"accordion", -6.0},
    {"brass", 6.0},
    {"kick", 6.0},
    {"snare", 0.0},
    {"other_drum", -6.0},
};
static_assert(sizeof(kStemLevels) / sizeof(kStemLevels[0]) == kStemCount,
              "每个声部都需要一行电平表");

constexpr bool in_range(int value, int first, int last) noexcept {
  return value >= first && value <= last;
}

constexpr std::size_t index_of(Stem stem) noexcept { return static_cast<std::size_t>(stem); }

bool is_drum_stem(Stem stem) noexcept {
  return stem == Stem::Kick || stem == Stem::Snare || stem == Stem::OtherDrum;
}

}  // namespace

const char* stem_name(Stem stem) noexcept { return kStemLevels[index_of(stem)].name; }

double stem_offset_db(Stem stem) noexcept { return kStemLevels[index_of(stem)].offset_db; }

double stem_gain_db(Stem stem, bool balance) noexcept {
  if (!balance) return 0.0;
  // 鼓组三个声部的偏移是绝对增益,不叠加统一基础电平。
  if (is_drum_stem(stem)) return stem_offset_db(stem);
  return kBalanceBaseDb + stem_offset_db(stem);
}

Stem classify_note(int channel, int program, int note) noexcept {
  if (channel == kDrumChannel) {
    for (const NoteRule& rule : kDrumRules) {
      if (in_range(note, rule.first, rule.last)) return rule.stem;
    }
    return Stem::OtherDrum;
  }
  for (const ProgramRule& rule : kProgramRules) {
    if (in_range(program, rule.first, rule.last)) return rule.stem;
  }
  return Stem::Melodic;
}

StemRouter::StemRouter(bool balance) noexcept : balance_(balance) {}

StemMask StemRouter::route(const MidiEvent& event) {
  const int channel = event.status & 0x0F;
  // 数据字节最高位无效,屏蔽后用作下标是安全的。
  const int data1 = event.data1 & 0x7F;
  switch (event.status & 0xF0) {
    case 0x80:
      return release(channel, data1);
    case 0x90: {
      if (event.data2 == 0) return release(channel, data1);
      const Stem stem =
          balance_ ? classify_note(channel, program_[channel], data1) : Stem::Melodic;
      active_[channel][data1][index_of(stem)] = true;
      return stem_bit(stem);
    }
    case 0xC0:
      program_[channel] = data1;
      return kAllStems;
    default:
      return kAllStems;
  }
}

StemMask StemRouter::release(int channel, int note) {
  StemMask mask = 0;
  auto& active = active_[channel][note];
  for (std::size_t s = 0; s < kStemCount; ++s) {
    if (active[s]) {
      mask = static_cast<StemMask>(mask | (1U << s));
      active[s] = false;
    }
  }
  return mask;
}

}  // namespace cgmusic
