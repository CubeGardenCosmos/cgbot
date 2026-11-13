#include "musicxml.hpp"

#include <algorithm>
#include <array>
#include <charconv>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <map>
#include <memory>
#include <optional>
#include <set>
#include <string>
#include <string_view>
#include <system_error>
#include <utility>
#include <vector>

#include <pugixml.hpp>
#include <zip.h>

#include "common.hpp"

namespace cgmusic {
namespace {

using Bytes = std::vector<std::uint8_t>;
using i64 = std::int64_t;
using xml_node = pugi::xml_node;

// 输出 SMF 分辨率与各类上限
constexpr i64 kPpq = 480;
constexpr std::size_t kMaxNotes = 500'000;
constexpr i64 kMaxRepeatWork = 1'000'000;  // 反复展开的小节实例上限
constexpr i64 kMaxRepeatTimes = 1000;
constexpr i64 kMaxDivisions = 1'000'000;
constexpr i64 kMaxDuration = 1'000'000'000;
constexpr i64 kMaxTicks = 0x0FFFFFFF;      // SMF 可变长度整数上限
constexpr std::size_t kMaxTracks = 0xFFFF;
constexpr i64 kGraceTicks = kPpq / 16;     // 装饰音不占时值
constexpr int kDrumChannel = 9;            // GM 打击乐通道(0 基)
constexpr int kDrumPitch = 38;             // 无映射时的默认打击乐音高
constexpr std::int64_t kDefaultTempoMicros = 500'000;  // 120 BPM
constexpr int kDefaultVelocity = 80;                   // mf
constexpr std::size_t kMaxTrackName = 127;

[[noreturn]] void fail(const char* message) { throw Error("bad_musicxml", message); }

// ---------- 字符串与数值工具 ----------

bool is_space(char c) { return c == ' ' || c == '\t' || c == '\n' || c == '\r'; }

std::string_view trim(std::string_view s) {
    while (!s.empty() && is_space(s.front())) s.remove_prefix(1);
    while (!s.empty() && is_space(s.back())) s.remove_suffix(1);
    return s;
}

std::string lower(std::string_view s) {
    std::string out(s);
    for (char& c : out) {
        if (c >= 'A' && c <= 'Z') c = static_cast<char>(c - 'A' + 'a');
    }
    return out;
}

i64 parse_int(std::string_view text, const char* message) {
    text = trim(text);
    if (text.empty()) fail(message);
    i64 value = 0;
    const char* first = text.data();
    const char* last = first + text.size();
    const auto [ptr, ec] = std::from_chars(first, last, value);
    if (ec != std::errc() || ptr != last) fail(message);
    return value;
}

// 拍数可写作 "3+2" 这样的复合拍子
i64 parse_beats(std::string_view text) {
    i64 total = 0;
    for (;;) {
        const std::size_t plus = text.find('+');
        total += parse_int(text.substr(0, plus), "Invalid time beats");
        if (plus == std::string_view::npos) return total;
        text.remove_prefix(plus + 1);
    }
}

double parse_number(std::string_view text, const char* message) {
    const std::string buffer(trim(text));
    if (buffer.empty()) fail(message);
    char* end = nullptr;
    const double value = std::strtod(buffer.c_str(), &end);
    if (end != buffer.c_str() + buffer.size() || !std::isfinite(value)) fail(message);
    return value;
}

bool valid_beat_type(i64 value) {
    return value >= 1 && value <= 128 && (value & (value - 1)) == 0;
}

// ---------- XML 访问 ----------

std::string_view name_of(xml_node node) { return node.name(); }

std::string_view value_of(xml_node node) { return trim(node.child_value()); }

std::string_view attr_of(xml_node node, const char* name) {
    return trim(node.attribute(name).as_string());
}

std::string_view child_value(xml_node node, const char* name) {
    xml_node child = node.child(name);
    return child ? value_of(child) : std::string_view{};
}

// ---------- 查表 ----------

struct NamedValue {
    std::string_view name;
    int value;
};

// 力度记号对应的 MIDI 力度
constexpr NamedValue kDynamics[] = {
    {"ppp", 28}, {"pp", 40}, {"p", 52}, {"mp", 64},
    {"mf", 80},  {"f", 96},  {"ff", 112}, {"fff", 124},
};

// 与原型一致的 GM 乐器表(0 基)。最长匹配优先,避免 english horn/contrabassoon 被短词误匹配。
constexpr NamedValue kPrograms[] = {
    {"piano", 0},      {"keyboard", 0},   {"organ", 19},     {"guitar", 24},
    {"bass", 32},      {"violin", 40},    {"viola", 41},     {"cello", 42},
    {"contrabass", 43}, {"string", 48},   {"choir", 52},     {"voice", 52},
    {"trumpet", 56},   {"trombone", 57},  {"tuba", 58},      {"horn", 60},
    {"sax", 65},       {"oboe", 68},      {"english horn", 69}, {"bassoon", 70},
    {"contrabassoon", 70}, {"clarinet", 71}, {"flute", 73},   {"recorder", 74},
    {"vibraphone", 11}, {"marimba", 12},  {"xylophone", 13},
};

constexpr std::string_view kDrumWords[] = {"drum", "percussion", "鼓", "打击"};

struct BeatUnit {
    std::string_view name;
    double quarters;
};

constexpr BeatUnit kBeatUnits[] = {
    {"maxima", 32.0},  {"long", 16.0},     {"breve", 8.0},     {"whole", 4.0},
    {"half", 2.0},     {"quarter", 1.0},   {"eighth", 0.5},    {"16th", 0.25},
    {"32nd", 0.125},   {"64th", 0.0625},   {"128th", 0.03125}, {"256th", 0.015625},
};

int dynamic_velocity(std::string_view name) {
    for (const NamedValue& entry : kDynamics) {
        if (entry.name == name) return entry.value;
    }
    return -1;
}

int guess_program(const std::string& lowered) {
    int best = 0;
    std::size_t best_len = 0;
    for (const NamedValue& entry : kPrograms) {
        if (entry.name.size() > best_len &&
            std::string_view(lowered).find(entry.name) != std::string_view::npos) {
            best = entry.value;
            best_len = entry.name.size();
        }
    }
    return best;
}

bool is_drum_name(const std::string& lowered) {
    for (std::string_view word : kDrumWords) {
        if (std::string_view(lowered).find(word) != std::string_view::npos) return true;
    }
    return false;
}

int velocity_from_percent(double percent) {
    // MusicXML <sound dynamics> 以 100 为基准,MIDI 力度按 127 缩放(与原型一致)
    const double clamped = std::clamp(percent, 0.0, 200.0);
    return std::clamp(static_cast<int>(std::lround(clamped * 1.27)), 1, 127);
}

int step_semitones(std::string_view step) {
    if (step.size() == 1) {
        switch (step[0]) {
            case 'C': return 0;
            case 'D': return 2;
            case 'E': return 4;
            case 'F': return 5;
            case 'G': return 7;
            case 'A': return 9;
            case 'B': return 11;
            default: break;
        }
    }
    fail("Invalid pitch step");
}

// 八度音高换算;超出 MIDI 范围即为坏输入
int midi_pitch(i64 octave, int step, int offset) {
    if (octave < -1 || octave > 10) fail("Note pitch out of MIDI range");
    const i64 value = (octave + 1) * 12 + step + offset;
    if (value < 0 || value > 127) fail("Note pitch out of MIDI range");
    return static_cast<int>(value);
}

// ---------- 数据模型 ----------

struct PartSource {
    std::string id;
    std::vector<xml_node> measures;  // partwise: <measure>;timewise: 该声部的 <part>
};

struct Declared {
    std::string name;
    std::optional<int> program;  // 0 基
    std::optional<int> channel;  // 0 基,仅记录有效的非打击乐声道
    bool drum = false;
    int list_rank = -1;         // 在 part-list 中的位置;-1 表示未列出
};

struct Declarations {
    std::map<std::string, Declared> parts;
    std::map<std::string, int> drum_pitch;  // instrument id -> MIDI 音高
};

struct TempoEv {
    i64 tick;
    std::int64_t micros;
};

struct SigEv {
    i64 tick;
    i64 beats;
    i64 beat_type;
};

struct KeyEv {
    i64 tick;
    int fifths;
    bool minor;
};

struct NoteEv {
    i64 start;
    i64 end;
    int pitch;
    int velocity;
};

// 拨奏切换事件(仅内部使用,在 musicxml_to_sequence 中填充到 MidiSequence)
struct PartPizzEv {
    i64 tick;    // 480 PPQ tick
    bool pizz;   // true = 拨奏, false = 弓奏
};

struct PartOut {
    std::string name;
    bool drum = false;
    int program = 0;
    std::optional<int> channel_hint;
    int channel = -1;
    int list_rank = -1;        // 在 part-list 中的位置;-1 表示未列出
    std::vector<NoteEv> notes;
    std::vector<KeyEv> keys;
    std::vector<TempoEv> tempos;
    std::vector<SigEv> sigs;
    std::vector<PartPizzEv> pizz_events;  // 拨奏/弓奏切换(tick 顺序)
};


struct Conductor {
    std::vector<TempoEv> tempos;
    std::vector<SigEv> sigs;
};

// 反复与跳房子标记
struct MeasureMarks {
    bool forward = false;       // 左反复线
    i64 backward_times = 0;     // >0 为右反复线的次数
    bool ending_start = false;  // 跳房子开始
    bool ending_stop = false;   // 跳房子结束(stop/discontinue)
    std::vector<i64> ending_numbers;
};

// 每个声部的播放状态(按播放顺序推进)
struct PartState {
    i64 divisions = 1;  // 缺省为 1:未声明 divisions 时按四分音符即 1 单位换算
    i64 beats = 4;
    i64 beat_type = 4;
    int transpose = 0;  // 半音,声部移调(chromatic + 12*octave-change)
    int velocity = kDefaultVelocity;
    bool pizz = false;  // 当前拨奏状态
    // (声部号, 音高) -> 尚未结束的连音音符下标
    std::map<std::pair<std::string, int>, std::size_t> open_ties;
};

// ---------- 压缩包(libzip,内存读取,无临时文件) ----------

struct ZipCloser {
    void operator()(zip_t* zip) const noexcept { zip_discard(zip); }
};
using ZipHandle = std::unique_ptr<zip_t, ZipCloser>;

struct FileCloser {
    void operator()(zip_file_t* file) const noexcept { zip_fclose(file); }
};
using FileHandle = std::unique_ptr<zip_file_t, FileCloser>;

struct EntryInfo {
    std::string name;
    zip_uint64_t index = 0;
    std::uint64_t size = 0;
};

// 打开内存中的 zip;data 由调用方持有,直到 archive 关闭为止
ZipHandle open_zip(const Bytes& data) {
    zip_error_t error;
    zip_error_init(&error);
    zip_source_t* source = zip_source_buffer_create(data.data(), data.size(), 0, &error);
    if (!source) {
        zip_error_fini(&error);
        fail("Invalid compressed MusicXML score");
    }
    zip_t* zip = zip_open_from_source(source, ZIP_RDONLY, &error);
    if (!zip) {
        zip_source_free(source);
        zip_error_fini(&error);
        fail("Invalid compressed MusicXML score");
    }
    zip_error_fini(&error);
    return ZipHandle(zip);
}

// 列出全部条目并累计未压缩大小(防止 zip 炸弹)
std::vector<EntryInfo> list_entries(zip_t* zip) {
    const zip_int64_t count = zip_get_num_entries(zip, 0);
    if (count < 0) fail("Invalid compressed MusicXML score");
    std::vector<EntryInfo> out;
    std::uint64_t total = 0;
    for (zip_uint64_t i = 0; i < static_cast<zip_uint64_t>(count); ++i) {
        zip_stat_t st;
        zip_stat_init(&st);
        const zip_uint64_t need = ZIP_STAT_NAME | ZIP_STAT_SIZE;
        if (zip_stat_index(zip, i, 0, &st) != 0 || (st.valid & need) != need || st.name == nullptr) {
            fail("Invalid compressed MusicXML score");
        }
        total += st.size;
        if (total > kMaxInputBytes) fail("Compressed score is too large");
        out.push_back(EntryInfo{std::string(st.name), i, st.size});
    }
    return out;
}

Bytes read_entry(zip_t* zip, const EntryInfo& entry) {
    FileHandle file(zip_fopen_index(zip, entry.index, 0));
    if (!file) fail("Unsupported compressed MusicXML entry");
    Bytes out;
    std::array<std::uint8_t, 64 * 1024> buffer{};
    for (;;) {
        // CRC 校验在读到文件末尾时由 zip_fread 报告
        const zip_int64_t got = zip_fread(file.get(), buffer.data(), buffer.size());
        if (got < 0) fail("Invalid compressed MusicXML entry");
        if (got == 0) break;
        const auto n = static_cast<std::size_t>(got);
        if (out.size() + n > kMaxInputBytes) fail("Compressed score is too large");
        out.insert(out.end(), buffer.data(), buffer.data() + n);
    }
    return out;
}

const EntryInfo* find_entry(const std::vector<EntryInfo>& entries, std::string_view name) {
    for (const EntryInfo& entry : entries) {
        if (std::string_view(entry.name) == name) return &entry;
    }
    return nullptr;
}

bool ends_with(const std::string& text, std::string_view suffix) {
    return text.size() >= suffix.size() &&
           std::string_view(text).substr(text.size() - suffix.size()) == suffix;
}

// container.xml 指定的 MusicXML 根文件;仅接受压缩包内真实存在的条目,找不到时返回空串
std::string rootfile_of(const Bytes& container_xml, const std::vector<std::string>& names) {
    pugi::xml_document doc;
    if (!doc.load_buffer(container_xml.data(), container_xml.size())) {
        fail("Invalid compressed MusicXML container");
    }
    const xml_node container = doc.document_element();
    if (!container) return {};
    for (xml_node rootfiles : container.children()) {
        if (name_of(rootfiles) != "rootfiles") continue;
        for (xml_node rootfile : rootfiles.children()) {
            if (name_of(rootfile) != "rootfile") continue;
            const std::string_view path = attr_of(rootfile, "full-path");
            const std::string_view media = attr_of(rootfile, "media-type");
            if (path.empty()) continue;
            if (!media.empty() && media.find("musicxml") == std::string_view::npos) continue;
            const std::string candidate(path);
            if (std::find(names.begin(), names.end(), candidate) != names.end()) return candidate;
        }
    }
    return {};
}

Bytes read_mxl(const Bytes& data) {
    const ZipHandle zip = open_zip(data);
    const std::vector<EntryInfo> entries = list_entries(zip.get());

    std::vector<std::string> names;
    names.reserve(entries.size());
    for (const EntryInfo& entry : entries) names.push_back(entry.name);

    const EntryInfo* score = nullptr;
    if (const EntryInfo* container = find_entry(entries, "META-INF/container.xml")) {
        const std::string path = rootfile_of(read_entry(zip.get(), *container), names);
        if (!path.empty()) score = find_entry(entries, path);
    }
    if (!score) {
        std::vector<const EntryInfo*> candidates;
        for (const EntryInfo& entry : entries) {
            const std::string lowered = lower(entry.name);
            const bool xml_like = ends_with(lowered, ".xml") || ends_with(lowered, ".musicxml");
            if (xml_like && entry.name.rfind("META-INF/", 0) != 0) candidates.push_back(&entry);
        }
        if (candidates.size() != 1) fail("Compressed score has no unique MusicXML file");
        score = candidates.front();
    }
    return read_entry(zip.get(), *score);
}

// ---------- 声部与结构 ----------

std::vector<PartSource> partwise_parts(xml_node root) {
    std::vector<PartSource> out;
    for (xml_node part : root.children()) {
        if (name_of(part) != "part") continue;
        PartSource src;
        src.id = std::string(attr_of(part, "id"));
        for (xml_node measure : part.children()) {
            if (name_of(measure) == "measure") src.measures.push_back(measure);
        }
        out.push_back(std::move(src));
    }
    return out;
}

// timewise: <measure> 内按声部排列;按声部重组为 partwise 结构
std::vector<PartSource> timewise_parts(xml_node root) {
    std::vector<PartSource> out;
    std::map<std::string, std::size_t> index;
    for (xml_node measure : root.children()) {
        if (name_of(measure) != "measure") continue;
        for (xml_node part : measure.children()) {
            if (name_of(part) != "part") continue;
            std::string id(attr_of(part, "id"));
            auto [it, inserted] = index.emplace(id, out.size());
            if (inserted) {
                PartSource src;
                src.id = std::move(id);
                out.push_back(std::move(src));
            }
            out[it->second].measures.push_back(part);
        }
    }
    return out;
}

Declared read_declared(xml_node part, std::map<std::string, int>& drum_pitch) {
    Declared declared;
    declared.name = std::string(child_value(part, "part-name"));
    for (xml_node instrument : part.children()) {
        const std::string_view tag = name_of(instrument);
        if (tag != "midi-instrument" && tag != "score-instrument") continue;
        const std::string_view instrument_id = attr_of(instrument, "id");
        for (xml_node item : instrument.children()) {
            const std::string_view item_tag = name_of(item);
            if (item_tag == "midi-program") {
                const i64 value = parse_int(value_of(item), "Invalid midi-program");
                if (value < 1 || value > 128) fail("Invalid midi-program");
                if (!declared.program) declared.program = static_cast<int>(value - 1);
            } else if (item_tag == "midi-channel") {
                const i64 value = parse_int(value_of(item), "Invalid midi-channel");
                if (value < 1 || value > 16) fail("Invalid midi-channel");
                if (value == 10) {
                    declared.drum = true;  // GM 打击乐通道
                } else if (!declared.channel) {
                    declared.channel = static_cast<int>(value - 1);
                }
            } else if (item_tag == "midi-unpitched") {
                const i64 value = parse_int(value_of(item), "Invalid midi-unpitched");
                if (value < 1 || value > 128) fail("Invalid midi-unpitched");
                declared.drum = true;
                if (!instrument_id.empty()) {
                    drum_pitch.emplace(std::string(instrument_id), static_cast<int>(value - 1));
                }
            } else if (item_tag == "percussion") {
                declared.drum = true;
            }
        }
    }
    if (is_drum_name(lower(declared.name))) declared.drum = true;
    return declared;
}

Declarations read_declarations(xml_node root) {
    Declarations out;
    const xml_node part_list = root.child("part-list");
    int rank = 0;
    for (xml_node score_part : part_list.children()) {
        if (name_of(score_part) != "score-part") continue;
        std::string id(attr_of(score_part, "id"));
        Declared declared = read_declared(score_part, out.drum_pitch);
        declared.list_rank = rank++;
        out.parts.emplace(std::move(id), std::move(declared));
    }
    return out;
}

// ---------- 时值 ----------

// divisions 单位的时值换算为 480 PPQ 的 tick(四舍五入)
i64 scale_divisions(i64 value, const PartState& st) {
    return (value * kPpq + st.divisions / 2) / st.divisions;
}

i64 duration_ticks(xml_node node, const PartState& st) {
    const xml_node duration = node.child("duration");
    if (!duration) fail("Missing duration");
    const i64 value = parse_int(value_of(duration), "Invalid duration");
    if (value < 0 || value > kMaxDuration) fail("Invalid duration");
    return scale_divisions(value, st);
}

i64 offset_ticks(xml_node direction, const PartState& st) {
    const xml_node offset = direction.child("offset");
    if (!offset) return 0;
    const i64 value = parse_int(value_of(offset), "Invalid offset");
    if (value > kMaxDuration || value < -kMaxDuration) fail("Invalid offset");
    return value < 0 ? -scale_divisions(-value, st) : scale_divisions(value, st);
}

// ---------- 反复与跳房子 ----------

std::vector<i64> ending_numbers_of(std::string_view text) {
    std::vector<i64> out;
    i64 current = 0;
    bool in_run = false;
    for (char c : text) {
        if (c >= '0' && c <= '9') {
            current = std::min<i64>(current * 10 + (c - '0'), 1'000'000'000);
            in_run = true;
        } else if (in_run) {
            out.push_back(current);
            current = 0;
            in_run = false;
        }
    }
    if (in_run) out.push_back(current);
    return out;
}

MeasureMarks marks_of(xml_node measure) {
    MeasureMarks marks;
    for (xml_node barline : measure.children()) {
        if (name_of(barline) != "barline") continue;
        for (xml_node item : barline.children()) {
            const std::string_view tag = name_of(item);
            if (tag == "repeat") {
                const std::string direction = lower(attr_of(item, "direction"));
                if (direction == "forward") {
                    marks.forward = true;
                } else if (direction == "backward") {
                    const std::string_view times = attr_of(item, "times");
                    i64 count = times.empty() ? 2 : parse_int(times, "Invalid repeat times");
                    if (count > kMaxRepeatTimes) fail("Repeat count is too large");
                    marks.backward_times = std::max<i64>(count, 1);
                }
            } else if (tag == "ending") {
                const std::string type = lower(attr_of(item, "type"));
                if (type == "start") {
                    marks.ending_start = true;
                    marks.ending_numbers = ending_numbers_of(attr_of(item, "number"));
                } else if (type == "stop" || type == "discontinue") {
                    marks.ending_stop = true;
                }
            }
        }
    }
    return marks;
}

bool contains_number(const std::vector<i64>& numbers, i64 value) {
    return std::find(numbers.begin(), numbers.end(), value) != numbers.end();
}

// 按播放顺序展开反复:返回要播放的小节下标序列。
// 反复段以左反复线或首小节开始;跳房子按当前遍数选择播放或跳过。
std::vector<std::size_t> repeat_plan(const std::vector<MeasureMarks>& marks) {
    std::vector<std::size_t> plan;
    const std::size_t count = marks.size();
    std::optional<std::size_t> active;  // 当前反复段的起点
    std::size_t section_begin = 0;      // 无左反复线时,右反复线回到此处
    i64 pass = 1;                       // 当前反复段的第几遍(1 基)
    i64 steps = 0;

    const auto close_if_done = [&](std::size_t j) {
        if (marks[j].ending_stop && (j + 1 >= count || !marks[j + 1].ending_start)) {
            section_begin = j + 1;
            active.reset();
            pass = 1;
        }
    };

    std::size_t i = 0;
    while (i < count) {
        if (++steps > kMaxRepeatWork) fail("Repeat expansion is too large");
        const MeasureMarks& m = marks[i];
        if (m.forward && (!active || *active != i)) {
            active = i;
            pass = 1;
        }
        if (m.ending_start && !m.ending_numbers.empty() && !contains_number(m.ending_numbers, pass)) {
            std::size_t stop = i;
            while (stop < count && !marks[stop].ending_stop) ++stop;
            if (stop < count) {
                close_if_done(stop);
                i = stop + 1;
                continue;
            }
        }
        plan.push_back(i);
        if (m.backward_times > 0) {
            if (!active) {
                active = section_begin;
                pass = 1;
            }
            if (pass < m.backward_times) {
                ++pass;
                i = *active;
                continue;
            }
            section_begin = i + 1;
            active.reset();
            pass = 1;
            ++i;
            continue;
        }
        close_if_done(i);
        ++i;
    }
    return plan;
}

// ---------- 音符与演奏 ----------

bool has_tie(xml_node note, std::string_view kind) {
    for (xml_node item : note.children()) {
        if (name_of(item) == "tie" && attr_of(item, "type") == kind) return true;
        if (name_of(item) == "notations") {
            for (xml_node tied : item.children()) {
                if (name_of(tied) == "tied" && attr_of(tied, "type") == kind) return true;
            }
        }
    }
    return false;
}

int pitch_of(xml_node note, bool drum, int transpose, const std::map<std::string, int>& drum_pitch) {
    if (drum) {
        const xml_node instrument = note.child("instrument");
        if (instrument) {
            const auto it = drum_pitch.find(std::string(attr_of(instrument, "id")));
            if (it != drum_pitch.end()) return it->second;
        }
        return kDrumPitch;
    }
    const xml_node pitch = note.child("pitch");
    if (pitch) {
        const int step = step_semitones(child_value(pitch, "step"));
        const i64 octave = parse_int(child_value(pitch, "octave"), "Invalid pitch octave");
        const xml_node alter = pitch.child("alter");
        double alter_value = 0.0;
        if (alter) alter_value = parse_number(value_of(alter), "Invalid pitch alter");
        if (std::fabs(alter_value) > 4.0) fail("Invalid pitch alter");
        const int semitones = static_cast<int>(std::lround(alter_value));
        return midi_pitch(octave, step, semitones + transpose);
    }
    const xml_node unpitched = note.child("unpitched");
    if (unpitched) {
        const std::string_view step_text = child_value(unpitched, "display-step");
        const int step = step_text.empty() ? 0 : step_semitones(step_text);
        const std::string_view octave_text = child_value(unpitched, "display-octave");
        const i64 octave = octave_text.empty() ? 4 : parse_int(octave_text, "Invalid display-octave");
        return midi_pitch(octave, step, 0);
    }
    fail("Note has no pitch");
}

int note_velocity(xml_node note, const PartState& st) {
    const std::string_view dynamics = attr_of(note, "dynamics");
    if (dynamics.empty()) return st.velocity;
    return velocity_from_percent(parse_number(dynamics, "Invalid note dynamics"));
}

void add_tempo(PartOut& out, i64 tick, double bpm) {
    if (!(bpm > 0.0)) fail("Tempo must be positive");
    const double micros = std::round(60'000'000.0 / bpm);
    if (micros < 1.0 || micros > 16'777'215.0) fail("Tempo out of range");
    out.tempos.push_back(TempoEv{tick, static_cast<std::int64_t>(micros)});
}

// <metronome>:每分钟拍数 × 拍值(含附点)换算为四分音符 BPM
std::optional<double> metronome_bpm(xml_node metronome) {
    const xml_node per_minute = metronome.child("per-minute");
    if (!per_minute) return std::nullopt;
    const double count = parse_number(value_of(per_minute), "Invalid per-minute");
    const std::string_view unit_name = child_value(metronome, "beat-unit");
    double quarters = 1.0;
    if (!unit_name.empty()) {
        const std::string lowered = lower(unit_name);
        bool found = false;
        for (const BeatUnit& unit : kBeatUnits) {
            if (unit.name == lowered) {
                quarters = unit.quarters;
                found = true;
                break;
            }
        }
        if (!found) fail("Invalid beat-unit");
    }
    int dots = 0;
    for (xml_node item : metronome.children()) {
        if (name_of(item) == "beat-unit-dot") ++dots;
    }
    return count * quarters * (2.0 - std::pow(0.5, dots));
}

// 拨奏/弓奏词:检测常见拼写,不分大小写(已对调用方使用 lower())
bool is_pizz_word(std::string_view word) {
    return word == "pizz." || word == "pizz";
}
bool is_arco_word(std::string_view word) {
    return word == "arco." || word == "arco";
}

// 若拨奏状态发生变化则追加切换事件。
void set_pizz(bool new_pizz, i64 tick, PartState& st, PartOut& out) {
    if (st.pizz != new_pizz) {
        st.pizz = new_pizz;
        out.pizz_events.push_back(PartPizzEv{tick, new_pizz});
    }
}

void apply_sound(xml_node sound, i64 tick, PartState& st, PartOut& out, std::optional<double>& tempo) {
    const std::string_view tempo_text = attr_of(sound, "tempo");
    if (!tempo_text.empty()) tempo = parse_number(tempo_text, "Invalid sound tempo");
    const std::string_view dynamics_text = attr_of(sound, "dynamics");
    if (!dynamics_text.empty()) {
        st.velocity = velocity_from_percent(parse_number(dynamics_text, "Invalid sound dynamics"));
    }
    // <sound pizzicato="yes|no"/>
    const std::string_view pizz_attr = attr_of(sound, "pizzicato");
    if (pizz_attr == "yes") set_pizz(true, tick, st, out);
    else if (pizz_attr == "no") set_pizz(false, tick, st, out);
}

void handle_sound(xml_node sound, i64 tick, PartState& st, PartOut& out) {
    std::optional<double> tempo;
    apply_sound(sound, tick, st, out, tempo);
    if (tempo) add_tempo(out, tick, *tempo);
}

void handle_direction(xml_node direction, i64 tick, PartState& st, PartOut& out) {
    std::optional<double> metronome;
    std::optional<double> sound_tempo;
    for (xml_node item : direction.children()) {
        const std::string_view tag = name_of(item);
        if (tag == "direction-type") {
            for (xml_node kind : item.children()) {
                const std::string_view kind_tag = name_of(kind);
                if (kind_tag == "dynamics") {
                    for (xml_node mark : kind.children()) {
                        const int velocity = dynamic_velocity(name_of(mark));
                        if (velocity > 0) st.velocity = velocity;
                    }
                } else if (kind_tag == "metronome") {
                    metronome = metronome_bpm(kind);
                } else if (kind_tag == "words") {
                    // 检测拨奏/弓奏指示词
                    const std::string word = lower(value_of(kind));
                    if (is_pizz_word(word)) set_pizz(true, tick, st, out);
                    else if (is_arco_word(word)) set_pizz(false, tick, st, out);
                }
            }
        } else if (tag == "sound") {
            apply_sound(item, tick, st, out, sound_tempo);
        }
    }
    if (metronome) {
        add_tempo(out, tick, *metronome);
    } else if (sound_tempo) {
        add_tempo(out, tick, *sound_tempo);
    }
}

void apply_attributes(xml_node attributes, i64 tick, PartState& st, PartOut& out) {
    if (const xml_node divisions = attributes.child("divisions")) {
        const i64 value = parse_int(value_of(divisions), "Invalid divisions");
        if (value < 1 || value > kMaxDivisions) fail("Invalid divisions");
        st.divisions = value;
    }
    if (const xml_node time = attributes.child("time")) {
        const xml_node beats = time.child("beats");
        const xml_node beat_type = time.child("beat-type");
        if (beats && beat_type) {
            const i64 count = parse_beats(value_of(beats));
            const i64 unit = parse_int(value_of(beat_type), "Invalid beat-type");
            if (count < 1 || count > 255 || !valid_beat_type(unit)) fail("Invalid time signature");
            st.beats = count;
            st.beat_type = unit;
            out.sigs.push_back(SigEv{tick, count, unit});
        }
    }
    if (const xml_node key = attributes.child("key")) {
        const i64 fifths = parse_int(child_value(key, "fifths"), "Invalid key fifths");
        if (fifths < -7 || fifths > 7) fail("Invalid key fifths");
        const bool minor = lower(child_value(key, "mode")) == "minor";
        out.keys.push_back(KeyEv{tick, static_cast<int>(fifths), minor});
    }
    if (const xml_node transpose = attributes.child("transpose")) {
        const std::string_view chromatic = child_value(transpose, "chromatic");
        const std::string_view octave = child_value(transpose, "octave-change");
        const i64 semitones = chromatic.empty() ? 0 : parse_int(chromatic, "Invalid transpose");
        const i64 octaves = octave.empty() ? 0 : parse_int(octave, "Invalid transpose");
        if (semitones < -48 || semitones > 48 || octaves < -4 || octaves > 4) {
            fail("Invalid transpose");
        }
        st.transpose = static_cast<int>(semitones + 12 * octaves);
    }
}

void play_note(xml_node note, i64& cursor, i64& last_onset, PartState& st, PartOut& out,
               const std::map<std::string, int>& drum_pitch, std::size_t& total) {
    const bool grace = static_cast<bool>(note.child("grace"));
    const bool chord = static_cast<bool>(note.child("chord"));
    const bool rest = static_cast<bool>(note.child("rest"));
    const bool cue = static_cast<bool>(note.child("cue"));
    const i64 length = grace ? kGraceTicks : duration_ticks(note, st);
    const i64 start = (chord && !grace) ? last_onset : cursor;

    // 检测 <notations><technical><pizzicato/> 触发拨奏;arco 仅靠 direction 切换
    for (xml_node notations : note.children()) {
        if (name_of(notations) != "notations") continue;
        for (xml_node tech : notations.children()) {
            if (name_of(tech) != "technical") continue;
            if (tech.child("pizzicato")) {
                set_pizz(true, start, st, out);
            }
        }
    }

    if (!rest && !cue) {
        const int pitch = pitch_of(note, out.drum, st.transpose, drum_pitch);
        const int velocity = note_velocity(note, st);
        const i64 end = start + std::max<i64>(1, length);
        const bool tie_start = !grace && has_tie(note, "start");
        const bool tie_stop = !grace && has_tie(note, "stop");
        std::string voice(child_value(note, "voice"));
        if (voice.empty()) voice = "1";
        const std::pair<std::string, int> key{voice, pitch};
        bool merged = false;
        if (tie_stop) {
            const auto it = st.open_ties.find(key);
            if (it != st.open_ties.end()) {
                NoteEv& open = out.notes[it->second];
                open.end = std::max(open.end, end);
                if (!tie_start) st.open_ties.erase(it);
                merged = true;
            }
        }
        if (!merged) {
            if (out.notes.size() >= kMaxNotes) fail("Score has too many notes");
            out.notes.push_back(NoteEv{start, end, pitch, velocity});
            if (tie_start) st.open_ties[key] = out.notes.size() - 1;
            ++total;
        }
    }
    if (!grace && !chord) {
        last_onset = cursor;
        cursor += length;
    }
}

// 演奏一个小节实例;返回小节内推进到的最远位置
i64 play_measure(xml_node measure, i64 start, PartState& st, PartOut& out,
                 const std::map<std::string, int>& drum_pitch, std::size_t& total) {
    i64 cursor = start;
    i64 furthest = start;
    i64 last_onset = start;
    for (xml_node item : measure.children()) {
        const std::string_view tag = name_of(item);
        if (tag == "attributes") {
            apply_attributes(item, cursor, st, out);
        } else if (tag == "direction") {
            handle_direction(item, cursor + offset_ticks(item, st), st, out);
        } else if (tag == "sound") {
            handle_sound(item, cursor, st, out);
        } else if (tag == "backup") {
            cursor -= duration_ticks(item, st);
            if (cursor < start) cursor = start;
        } else if (tag == "forward") {
            cursor += duration_ticks(item, st);
        } else if (tag == "note") {
            play_note(item, cursor, last_onset, st, out, drum_pitch, total);
        }
        furthest = std::max(furthest, cursor);
        if (furthest > kMaxTicks) fail("Score is too long");
    }
    return furthest;
}

void play_part(PartOut& out, const std::vector<xml_node>& measures,
               const std::map<std::string, int>& drum_pitch, std::size_t& total) {
    std::vector<MeasureMarks> marks;
    marks.reserve(measures.size());
    for (xml_node measure : measures) marks.push_back(marks_of(measure));
    const std::vector<std::size_t> plan = repeat_plan(marks);

    PartState st;
    i64 now = 0;
    for (std::size_t index : plan) {
        const i64 end = play_measure(measures[index], now, st, out, drum_pitch, total);
        i64 length = end - now;
        if (length <= 0) length = st.beats * 4 * kPpq / st.beat_type;  // 空小节按拍号计
        now += length;
        if (now > kMaxTicks) fail("Score is too long");
    }
}

// ---------- 声道分配 ----------

void assign_channels(std::vector<PartOut>& parts) {
    // 声道分配顺序:part-list 中列出的声部按列表顺序优先,其余按文档顺序
    std::vector<PartOut*> order;
    order.reserve(parts.size());
    for (PartOut& part : parts) order.push_back(&part);
    std::stable_sort(order.begin(), order.end(), [](const PartOut* a, const PartOut* b) {
        const bool listed_a = a->list_rank >= 0;
        const bool listed_b = b->list_rank >= 0;
        if (listed_a != listed_b) return listed_a;
        return a->list_rank < b->list_rank;
    });

    std::array<bool, 16> used{};
    for (PartOut* part : order) {
        if (part->drum) part->channel = kDrumChannel;
    }
    for (PartOut* part : order) {
        if (part->drum || !part->channel_hint) continue;
        const int channel = *part->channel_hint;
        if (channel != kDrumChannel && !used[channel]) {
            part->channel = channel;
            used[channel] = true;
        }
    }
    std::vector<int> pitched;
    for (int channel = 0; channel < 16; ++channel) {
        if (channel != kDrumChannel) pitched.push_back(channel);
    }
    std::size_t next_free = 0;
    std::size_t overflow = 0;
    for (PartOut* part : order) {
        if (part->drum || part->channel >= 0) continue;
        while (next_free < pitched.size() && used[pitched[next_free]]) ++next_free;
        if (next_free < pitched.size()) {
            part->channel = pitched[next_free];
            used[pitched[next_free]] = true;
        } else {
            // 非打击乐声部超过 15 个时循环复用声道
            part->channel = pitched[overflow % pitched.size()];
            ++overflow;
        }
    }
}

// ---------- SMF 编码 ----------

void put_vlq(Bytes& out, std::uint64_t value) {
    std::array<std::uint8_t, 10> bytes{};
    std::size_t count = 0;
    do {
        bytes[count++] = static_cast<std::uint8_t>(value & 0x7F);
        value >>= 7;
    } while (value != 0);
    for (std::size_t i = count; i-- > 0;) {
        out.push_back(static_cast<std::uint8_t>(bytes[i] | (i != 0 ? 0x80 : 0x00)));
    }
}

void put_be16(Bytes& out, std::uint32_t value) {
    out.push_back(static_cast<std::uint8_t>(value >> 8));
    out.push_back(static_cast<std::uint8_t>(value));
}

void put_be32(Bytes& out, std::uint32_t value) {
    put_be16(out, value >> 16);
    put_be16(out, value & 0xFFFF);
}

void put_meta(Bytes& out, std::uint8_t type, const Bytes& payload) {
    out.push_back(0xFF);
    out.push_back(type);
    put_vlq(out, payload.size());
    out.insert(out.end(), payload.begin(), payload.end());
}

std::string truncate_utf8(std::string text, std::size_t limit) {
    if (text.size() <= limit) return text;
    std::size_t cut = limit;
    while (cut > 0 && (static_cast<unsigned char>(text[cut]) & 0xC0) == 0x80) --cut;
    text.resize(cut);
    return text;
}

enum class EvKind : std::uint8_t { Tempo, Signature, Key, NoteOff, NoteOn };

struct Ev {
    i64 tick;
    int prio;  // 同刻顺序:元信息 < note-off < note-on
    EvKind kind;
    int x;
    int y;
    int z;
    Ev(i64 t, int p, EvKind k, int a, int b, int c) : tick(t), prio(p), kind(k), x(a), y(b), z(c) {}
};

void encode_track(Bytes& out, const PartOut& part, const Conductor* conductor) {
    std::vector<Ev> events;
    if (conductor) {
        for (const TempoEv& tempo : conductor->tempos) {
            events.emplace_back(tempo.tick, 0, EvKind::Tempo, static_cast<int>(tempo.micros), 0, 0);
        }
        for (const SigEv& sig : conductor->sigs) {
            events.emplace_back(sig.tick, 0, EvKind::Signature, static_cast<int>(sig.beats),
                                static_cast<int>(sig.beat_type), 0);
        }
    }
    for (const KeyEv& key : part.keys) {
        events.emplace_back(key.tick, 0, EvKind::Key, key.fifths, key.minor ? 1 : 0, 0);
    }
    for (const NoteEv& note : part.notes) {
        events.emplace_back(note.start, 2, EvKind::NoteOn, part.channel, note.pitch, note.velocity);
        events.emplace_back(note.end, 1, EvKind::NoteOff, part.channel, note.pitch, 0);
    }
    std::stable_sort(events.begin(), events.end(), [](const Ev& a, const Ev& b) {
        return a.tick != b.tick ? a.tick < b.tick : a.prio < b.prio;
    });

    Bytes track;
    const std::string name = truncate_utf8(part.name, kMaxTrackName);
    put_vlq(track, 0);
    put_meta(track, 0x03, Bytes(name.begin(), name.end()));
    if (!part.drum) {
        put_vlq(track, 0);
        track.push_back(static_cast<std::uint8_t>(0xC0 | part.channel));
        track.push_back(static_cast<std::uint8_t>(part.program));
    }

    i64 previous = 0;
    for (const Ev& ev : events) {
        if (ev.tick > kMaxTicks) throw Error("too_long", "Score is too long");
        put_vlq(track, static_cast<std::uint64_t>(ev.tick - previous));
        previous = ev.tick;
        switch (ev.kind) {
            case EvKind::Tempo: {
                const auto micros = static_cast<std::uint32_t>(ev.x);
                put_meta(track, 0x51,
                         Bytes{static_cast<std::uint8_t>(micros >> 16),
                               static_cast<std::uint8_t>(micros >> 8),
                               static_cast<std::uint8_t>(micros)});
                break;
            }
            case EvKind::Signature: {
                int denominator_power = 0;
                while ((1 << denominator_power) < ev.y) ++denominator_power;
                put_meta(track, 0x58,
                         Bytes{static_cast<std::uint8_t>(ev.x),
                               static_cast<std::uint8_t>(denominator_power), 24, 8});
                break;
            }
            case EvKind::Key:
                put_meta(track, 0x59,
                         Bytes{static_cast<std::uint8_t>(static_cast<std::int8_t>(ev.x)),
                               static_cast<std::uint8_t>(ev.y)});
                break;
            case EvKind::NoteOff:
                track.push_back(static_cast<std::uint8_t>(0x80 | ev.x));
                track.push_back(static_cast<std::uint8_t>(ev.y));
                track.push_back(0);
                break;
            case EvKind::NoteOn:
                track.push_back(static_cast<std::uint8_t>(0x90 | ev.x));
                track.push_back(static_cast<std::uint8_t>(ev.y));
                track.push_back(static_cast<std::uint8_t>(ev.z));
                break;
        }
    }
    // 轨道结束点取最后一个事件的刻,使 midi_duration 反映最后音符结束时刻
    put_vlq(track, 0);
    track.push_back(0xFF);
    track.push_back(0x2F);
    track.push_back(0x00);

    if (track.size() > 0xFFFFFFFFULL) throw Error("too_long", "Score is too long");
    out.push_back('M');
    out.push_back('T');
    out.push_back('r');
    out.push_back('k');
    put_be32(out, static_cast<std::uint32_t>(track.size()));
    out.insert(out.end(), track.begin(), track.end());
}

Bytes encode_smf(const std::vector<PartOut>& parts, const Conductor& conductor) {
    Bytes out;
    for (const char c : {'M', 'T', 'h', 'd'}) out.push_back(static_cast<std::uint8_t>(c));
    put_be32(out, 6);
    put_be16(out, 1);
    put_be16(out, static_cast<std::uint32_t>(parts.size()));
    put_be16(out, static_cast<std::uint32_t>(kPpq));
    for (std::size_t i = 0; i < parts.size(); ++i) {
        encode_track(out, parts[i], i == 0 ? &conductor : nullptr);
    }
    if (out.size() > kMaxMidiBytes) throw Error("too_long", "MIDI output is too large");
    return out;
}

template <typename T>
void settle_by_tick(std::vector<T>& items) {
    std::stable_sort(items.begin(), items.end(), [](const T& a, const T& b) { return a.tick < b.tick; });
    std::vector<T> kept;
    kept.reserve(items.size());
    for (std::size_t i = 0; i < items.size(); ++i) {
        // 同刻多次设定时保留最后一次(后出现的声部覆盖先出现的)
        if (i + 1 < items.size() && items[i + 1].tick == items[i].tick) continue;
        kept.push_back(items[i]);
    }
    items.swap(kept);
}

bool is_zip_data(const Bytes& data) {
    return data.size() >= 4 && std::memcmp(data.data(), "PK\x03\x04", 4) == 0;
}

}  // namespace

// 解析 MusicXML 并返回声部列表与指挥轨道(共享实现)。
// 调用方负责后续的 encode_smf 或序列化。
static void parse_xml_parts(const std::vector<std::uint8_t>& data, std::string_view filename,
                             std::vector<PartOut>& parts, Conductor& conductor) {
    Bytes unpacked;
    const Bytes* xml = &data;
    const std::string lowered_name = lower(filename);
    if (is_zip_data(data) || ends_with(lowered_name, ".mxl")) {
        unpacked = read_mxl(data);
        xml = &unpacked;
    }
    if (xml->size() > kMaxInputBytes) fail("MusicXML score is too large");

    pugi::xml_document doc;
    if (!doc.load_buffer(xml->data(), xml->size())) fail("Invalid MusicXML document");
    const xml_node root = doc.document_element();

    std::vector<PartSource> sources;
    if (name_of(root) == "score-timewise") {
        sources = timewise_parts(root);
    } else if (name_of(root) == "score-partwise") {
        sources = partwise_parts(root);
    } else {
        fail("Expected a score-partwise MusicXML document");
    }
    if (sources.empty()) fail("Score has no parts");
    if (sources.size() > kMaxTracks) fail("Score has too many parts");
    {
        std::set<std::string> seen;
        for (const PartSource& src : sources) {
            if (!seen.insert(src.id).second) fail("Duplicate part id");
        }
    }

    const Declarations decl = read_declarations(root);
    parts.reserve(sources.size());
    std::size_t total_notes = 0;
    for (const PartSource& src : sources) {
        const auto found = decl.parts.find(src.id);
        const Declared* declared = found == decl.parts.end() ? nullptr : &found->second;

        PartOut part;
        part.name = (declared && !declared->name.empty()) ? declared->name : src.id;
        part.drum = (declared && declared->drum) || is_drum_name(lower(part.name));
        part.channel_hint = declared ? declared->channel : std::nullopt;
        part.list_rank = declared ? declared->list_rank : -1;
        if (declared && declared->program) {
            part.program = *declared->program;
        } else {
            part.program = guess_program(lower(part.name));
        }
        play_part(part, src.measures, decl.drum_pitch, total_notes);
        parts.push_back(std::move(part));
    }
    assign_channels(parts);

    conductor.tempos.push_back(TempoEv{0, kDefaultTempoMicros});
    conductor.sigs.push_back(SigEv{0, 4, 4});
    for (const PartOut& part : parts) {
        conductor.tempos.insert(conductor.tempos.end(), part.tempos.begin(), part.tempos.end());
        conductor.sigs.insert(conductor.sigs.end(), part.sigs.begin(), part.sigs.end());
    }
    settle_by_tick(conductor.tempos);
    settle_by_tick(conductor.sigs);
}

std::vector<std::uint8_t> musicxml_to_midi(const std::vector<std::uint8_t>& data,
                                           std::string_view filename) {
    std::vector<PartOut> parts;
    Conductor conductor;
    parse_xml_parts(data, filename, parts, conductor);
    return encode_smf(parts, conductor);
}

MidiSequence musicxml_to_sequence(const std::vector<std::uint8_t>& data,
                                  std::string_view filename) {
    std::vector<PartOut> parts;
    Conductor conductor;
    parse_xml_parts(data, filename, parts, conductor);

    // 基于 conductor.tempos 建立 tick -> seconds 映射(与 480 PPQ 速度表算法完全一致)
    struct TempoSeg {
        i64 tick;
        double seconds;
        double micros;
    };
    std::vector<TempoSeg> segs;
    constexpr double kPpqD = 480.0;
    segs.push_back({0, 0.0, static_cast<double>(kDefaultTempoMicros)});
    for (const TempoEv& t : conductor.tempos) {
        if (t.tick == 0) {
            segs[0].micros = static_cast<double>(t.micros);
            continue;
        }
        TempoSeg& last = segs.back();
        const double dtick = static_cast<double>(t.tick - last.tick);
        const double dsec = dtick * (last.micros / (kPpqD * 1'000'000.0));
        if (t.tick == last.tick) {
            last.micros = static_cast<double>(t.micros);
        } else {
            segs.push_back({t.tick, last.seconds + dsec, static_cast<double>(t.micros)});
        }
    }
    const auto tick_to_seconds = [&](i64 tick) -> double {
        const auto it = std::upper_bound(segs.begin(), segs.end(), tick,
            [](i64 val, const TempoSeg& s) { return val < s.tick; });
        const TempoSeg& s = *(it - 1);
        return s.seconds + static_cast<double>(tick - s.tick) * (s.micros / (kPpqD * 1'000'000.0));
    };

    MidiSequence sequence;
    for (const auto& s : segs) {
        sequence.tempo_map.push_back({static_cast<std::uint64_t>(s.tick), s.seconds,
                                      static_cast<std::uint32_t>(std::lround(s.micros))});
    }

    const std::size_t nparts = parts.size();
    sequence.channel_names.resize(nparts);
    sequence.channel_is_drum.resize(nparts);
    sequence.pizzicato_ranges.resize(nparts);

    double max_time = 0.0;
    for (std::size_t i = 0; i < nparts; ++i) {
        const PartOut& part = parts[i];
        const int ch = static_cast<int>(i);
        sequence.channel_names[i] = part.name;
        sequence.channel_is_drum[i] = part.drum;

        // 程序变更事件(刻 0)
        const std::uint8_t status_pc = static_cast<std::uint8_t>(0xC0 | (i & 0x0F));
        sequence.events.push_back({0.0, status_pc, static_cast<std::uint8_t>(part.program), 0, ch});

        // 音符开关
        for (const NoteEv& note : part.notes) {
            const double t_start = tick_to_seconds(note.start);
            const double t_end = tick_to_seconds(note.end);
            max_time = std::max(max_time, t_end);

            const std::uint8_t status_on = static_cast<std::uint8_t>(0x90 | (i & 0x0F));
            const std::uint8_t status_off = static_cast<std::uint8_t>(0x80 | (i & 0x0F));
            sequence.events.push_back({t_start, status_on, static_cast<std::uint8_t>(note.pitch),
                                       static_cast<std::uint8_t>(note.velocity), ch});
            sequence.events.push_back({t_end, status_off, static_cast<std::uint8_t>(note.pitch), 0, ch});
        }

        // 拨奏事件
        for (const PartPizzEv& pev : part.pizz_events) {
            sequence.pizzicato_ranges[i].push_back({tick_to_seconds(pev.tick), pev.pizz});
        }
    }

    sequence.duration_s = max_time;
    // 按时间排序;同刻先 note-off,再 PC,最后 note-on
    std::stable_sort(sequence.events.begin(), sequence.events.end(),
                     [](const MidiEvent& a, const MidiEvent& b) {
                         if (a.seconds != b.seconds) return a.seconds < b.seconds;
                         const int prio_a = (a.status & 0xF0) == 0x80 ? 0 : ((a.status & 0xF0) == 0xC0 ? 1 : 2);
                         const int prio_b = (b.status & 0xF0) == 0x80 ? 0 : ((b.status & 0xF0) == 0xC0 ? 1 : 2);
                         return prio_a < prio_b;
                     });

    return sequence;
}

}  // namespace cgmusic
