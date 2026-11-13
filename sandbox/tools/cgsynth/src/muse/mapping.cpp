#include "muse/mapping.hpp"

#include <algorithm>
#include <cctype>
#include <string>
#include <string_view>

namespace cgmusic {
namespace {

// 候选音色表;表内顺序即优先级(同一程序的多行按行序尝试)。
// program 为闭区间(0 基 GM 程序号);drum 行只匹配鼓组通道,忽略 program。
// name 为精确名称;abbr 仅在目录中恰好一个乐器使用该缩写时作为回退。
// name 为 nullptr 的行是保留项:该程序整体留给 FluidSynth(例如 GM 4、5 电钢琴,默认 DX7 音色)。
struct Candidate {
  bool drum;
  int first;
  int last;
  const char* name;
  const char* abbr;
};

constexpr Candidate kCandidates[] = {
    // 鼓组通道(MIDI 通道 10):Drum Kit 优先。实测本机 0.106.0 渲染 GM Kit 时库内抛出 invalid_argument("Invalid file name")并 terminate,Drum Kit 正常。
    {true, 0, 127, "Drum Kit", nullptr},
    {true, 0, 127, "GM Kit", nullptr},

    // 钢琴、键盘与打击键盘
    {false, 0, 2, "Piano", nullptr},
    {false, 3, 3, "Upright Piano", nullptr},
    {false, 3, 3, "Piano", nullptr},
    {false, 4, 5, nullptr, nullptr},  // 电钢琴:保留 FluidSynth 的 DX7 音色
    {false, 6, 6, "Harpsichord", "Hrpchd."},
    {false, 8, 8, "Celesta", "Cel."},
    {false, 9, 9, "Glockenspiel", "Glock."},
    {false, 10, 10, "Jewellery Music Box", nullptr},
    {false, 10, 10, "Joana's Music Box", nullptr},
    {false, 11, 11, "Vibraphone", "Vibr."},
    {false, 12, 12, "Marimba", "Marim."},
    {false, 13, 13, "Xylophone", "Xyl."},
    {false, 14, 14, "Tubular Bells", "T. Bell"},
    {false, 16, 18, "Hammond Organ", "Org."},

    // 吉他与贝斯
    {false, 24, 24, "Acoustic Nylon", nullptr},
    {false, 25, 25, "Acoustic Steel Picked", nullptr},
    {false, 25, 25, "Acoustic Steel Plucked", nullptr},
    {false, 26, 26, "Electric SC - Clean", nullptr},
    {false, 27, 28, "Electric LP - Clean", nullptr},
    {false, 29, 30, "Electric LP - Heavy", nullptr},
    {false, 31, 31, "Electric SC - Clean", nullptr},
    {false, 33, 36, "Electric Bass", nullptr},

    // 弦乐:独奏优先于声部
    {false, 40, 40, "Violin 1 Solo", nullptr},
    {false, 40, 40, "Violins 1", nullptr},
    {false, 41, 41, "Viola Solo", nullptr},
    {false, 41, 41, "Violas", nullptr},
    {false, 42, 42, "Cello Solo", nullptr},
    {false, 42, 42, "Cellos", nullptr},
    {false, 43, 43, "Contrabasses", nullptr},
    {false, 44, 45, "Violins 1", nullptr},
    {false, 46, 46, "Harp", "Hp."},
    {false, 47, 47, "Timpani", "Timp."},
    {false, 48, 48, "Violins 1", nullptr},

    // 合唱
    {false, 52, 54, "Full Choir", nullptr},

    // 铜管
    {false, 56, 56, "Trumpet", "Tpt."},
    {false, 57, 57, "Trombone", "Tbn."},
    {false, 58, 58, "Tuba", "Tba."},
    {false, 59, 59, "Trumpet", "Tpt."},
    {false, 60, 60, "French Horn", "Hn"},
    {false, 61, 61, "Trumpets a4", "Tpts."},

    // 木管
    {false, 64, 64, "Soprano Saxophone", "S. Sax."},
    {false, 65, 65, "Alto Saxophone", "A. Sax."},
    {false, 66, 66, "Tenor Saxophone", "T. Sax."},
    {false, 67, 67, "Baritone Saxophone", "B. Sax."},
    {false, 68, 68, "Oboe", "Ob."},
    {false, 69, 69, "English Horn", "E. Hn."},
    {false, 70, 70, "Bassoon", "Bsn."},
    {false, 71, 71, "Clarinet in Bb", "Cl. in Bb"},
    {false, 72, 72, "Piccolo", "Picc."},
    {false, 73, 73, "Flute 1", nullptr},

    // 民族与打击乐
    {false, 113, 113, "Agogos", nullptr},
    {false, 115, 115, "Wood Blocks", nullptr},
    {false, 116, 116, "Taikos", nullptr},
    {false, 117, 117, "Toms", nullptr},
};

bool covers(const Candidate& candidate, int gm_program, bool is_drum) noexcept {
  if (candidate.drum != is_drum) return false;
  return is_drum || (gm_program >= candidate.first && gm_program <= candidate.last);
}

// 精确名称优先;缩写仅在目录中唯一匹配时采用,避免歧义。
std::optional<int> find_candidate(const Catalog& catalog, const Candidate& candidate) {
  if (candidate.name != nullptr) {
    const auto it = std::find_if(catalog.begin(), catalog.end(), [&](const MuseInstrument& item) {
      return item.name == candidate.name;
    });
    if (it != catalog.end()) return it->id;
  }
  if (candidate.abbr != nullptr) {
    const std::string_view abbr = candidate.abbr;
    const auto first = std::find_if(catalog.begin(), catalog.end(), [&](const MuseInstrument& item) {
      return item.abbreviation == abbr;
    });
    if (first != catalog.end() &&
        std::count_if(first, catalog.end(), [&](const MuseInstrument& item) {
          return item.abbreviation == abbr;
        }) == 1) {
      return first->id;
    }
  }
  return std::nullopt;
}

}  // namespace

std::optional<int> pick_instrument(int gm_program, bool is_drum, const Catalog& catalog) {
  // 保留项整体优先:即使表中有其他候选覆盖同一程序,也交给 FluidSynth。
  for (const Candidate& candidate : kCandidates) {
    if (candidate.name == nullptr && candidate.abbr == nullptr &&
        covers(candidate, gm_program, is_drum)) {
      return std::nullopt;
    }
  }
  for (const Candidate& candidate : kCandidates) {
    if (!covers(candidate, gm_program, is_drum)) continue;
    if (const auto id = find_candidate(catalog, candidate)) return id;
  }
  return std::nullopt;
}
std::optional<int> find_exact_instrument(const Catalog& catalog, std::string_view name) {
  if (name.empty()) return std::nullopt;
  const auto it = std::find_if(catalog.begin(), catalog.end(),
                               [&](const MuseInstrument& item) { return item.name == name; });
  return it == catalog.end() ? std::nullopt : std::optional<int>(it->id);
}


}  // namespace cgmusic
