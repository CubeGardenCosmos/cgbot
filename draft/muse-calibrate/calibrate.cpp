#include "muse/calibrate.hpp"

#include <algorithm>
#include <cctype>
#include <chrono>
#include <cmath>
#include <ctime>
#include <iomanip>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <unordered_map>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#include "common.hpp"
#include "encode.hpp"
#include "midi.hpp"
#include "muse/catalog.hpp"
#include "muse/loader.hpp"
#include "muse/render.hpp"
#include "render.hpp"
#include "soundfont.hpp"

namespace fs = std::filesystem;

namespace cgmusic::muse {
namespace {

struct InstrumentTargetDef {
  const char* name;
  const char* family;
  int pitch;
  double target_mf;
  double uncertainty_db;
  const char* source;
};

constexpr const char* kDefaultLibrary = "/opt/musesampler/lib/libMuseSamplerCoreLib.so";
constexpr const char* kDefaultSamples = "/opt/muse-sounds";

std::string env_or(const char* name, const char* fallback) {
  const char* val = std::getenv(name);
  return val != nullptr && *val != '\0' ? val : fallback;
}
// 3. 管弦乐平衡与非相干声功率叠加规则 (10 log10 N)
constexpr InstrumentTargetDef kTargetDefs[] = {
    // 木管
    {"Flute 1", "woodwinds", 72, -33.0, 1.0, "Meyer (2009) ch. 4; woodwind reference anchor"},
    {"Flute 2", "woodwinds", 72, -33.0, 1.0, "Meyer (2009) ch. 4; woodwind reference anchor"},
    {"Piccolo", "woodwinds", 84, -32.0, 1.0, "Meyer (2009) ch. 4; high ear sensitivity, octave projection"},
    {"Alto Flute", "woodwinds", 67, -34.0, 1.0, "Meyer (2009) ch. 4; diffuse lower register"},
    {"Bass Flute", "woodwinds", 60, -35.0, 1.5, "Meyer (2009) ch. 4; low acoustic power"},
    {"Clarinet in Bb", "woodwinds", 67, -33.0, 1.0, "Meyer (2009) ch. 4; dynamic flexibility, woodwind anchor"},
    {"Clarinet in Eb", "woodwinds", 72, -32.5, 1.0, "Meyer (2009) ch. 4; soprano projection"},
    {"Bass Clarinet", "woodwinds", 50, -34.0, 1.0, "Meyer (2009) ch. 4; chalumeau register warmth"},
    {"Oboe", "woodwinds", 69, -32.5, 1.0, "Meyer (2009) ch. 4; prominent formants at 1.4 & 3.0 kHz"},
    {"English Horn", "woodwinds", 62, -33.0, 1.0, "Meyer (2009) ch. 4; alto reed projection"},
    {"Bassoon", "woodwinds", 48, -34.5, 1.0, "Meyer (2009) ch. 4; lower register, rapid high roll-off"},
    {"Contrabassoon", "woodwinds", 36, -35.0, 1.5, "Meyer (2009) ch. 4; deep foundation"},
    {"Soprano Saxophone", "woodwinds", 69, -32.0, 1.0, "Meyer (2009) ch. 4; conical bore reed"},
    {"Alto Saxophone", "woodwinds", 64, -32.0, 1.0, "Meyer (2009) ch. 4; conical bore reed"},
    {"Tenor Saxophone", "woodwinds", 57, -32.5, 1.0, "Meyer (2009) ch. 4; conical bore reed"},
    {"Baritone Saxophone", "woodwinds", 52, -33.0, 1.0, "Meyer (2009) ch. 4; conical bore reed"},

    // 铜管
    {"French Horn", "brass", 53, -33.0, 1.0, "Meyer (2009) ch. 5; rear-facing bell creates diffuse warmth"},
    {"French Horns a6", "brass", 53, -28.0, 1.5, "Meyer (2009) ch. 5; 6-horn section balance (+7.8 dB uncorr.)"},
    {"Trumpet", "brass", 67, -31.0, 1.0, "Meyer (2009) ch. 5; directional bell, forward projection"},
    {"Trumpets a4", "brass", 67, -27.0, 1.5, "Meyer (2009) ch. 5; 4-trumpet section balance (+6.0 dB uncorr.)"},
    {"Trombone", "brass", 53, -31.0, 1.0, "Meyer (2009) ch. 5; cylindrical/conical brass projection"},
    {"Trombones a3", "brass", 53, -27.5, 1.5, "Meyer (2009) ch. 5; 3-trombone section balance (+4.8 dB uncorr.)"},
    {"Bass Trombone", "brass", 48, -31.5, 1.0, "Meyer (2009) ch. 5; low brass projection"},
    {"Euphonium", "brass", 53, -32.0, 1.0, "Meyer (2009) ch. 5; wide conical low brass"},
    {"Tuba", "brass", 41, -33.0, 1.0, "Meyer (2009) ch. 5; upward radiating bell foundation"},
    {"Cimbasso", "brass", 41, -32.5, 1.5, "Meyer (2009) ch. 5; directional contrabass trombone"},

    // 弦乐 (独奏与声部,声部非相干叠加 10 log10 N)
    {"Violin 1 Solo", "strings", 69, -35.0, 1.0, "Meyer (2009) ch. 6; solo violin at 10m L_W ~80-83 dB"},
    {"Violin 2 Solo", "strings", 69, -35.0, 1.0, "Meyer (2009) ch. 6; solo violin"},
    {"Violins 1", "strings", 69, -28.5, 1.5, "Meyer (2009) ch. 6; ~16 players (+12 dB over solo, orchestral hall balance)"},
    {"Violins 2", "strings", 69, -29.5, 1.5, "Meyer (2009) ch. 6; ~14 players (+11.5 dB, ~1 dB below Vln 1)"},
    {"Viola Solo", "strings", 62, -35.0, 1.0, "Meyer (2009) ch. 6; solo viola"},
    {"Violas", "strings", 62, -30.0, 1.5, "Meyer (2009) ch. 6; ~12 players (+10.8 dB over solo)"},
    {"Cello Solo", "strings", 50, -34.0, 1.0, "Meyer (2009) ch. 6; solo cello L_W ~81-84 dB"},
    {"Cellos", "strings", 50, -29.0, 1.5, "Meyer (2009) ch. 6; ~10 players (+10 dB over solo)"},
    {"Contrabasses", "strings", 38, -30.0, 1.5, "Meyer (2009) ch. 6; ~8 players (+9 dB over solo, room reinforcement)"},

    // 键盘与弹拨乐
    {"Piano", "keys", 60, -29.0, 1.5, "Fletcher & Rossing (1998) ch. 12; grand piano orchestral listener balance"},
    {"Upright Piano", "keys", 60, -30.0, 1.5, "Fletcher & Rossing (1998) ch. 12; upright piano acoustics"},
    {"Harpsichord", "keys", 60, -32.0, 1.0, "Meyer (2009) ch. 6; plucked keyboard"},
    {"Celesta", "keys", 72, -33.0, 1.0, "Meyer (2009) ch. 6; steel plates high harmonic sensitivity"},
    {"Hammond Organ", "keys", 60, -30.0, 1.5, "Tonewheel organ listener perspective"},
    {"Harp", "harp", 60, -33.0, 1.0, "Meyer (2009) ch. 6; plucked strings delicate sound power"},

    // 打击乐与鼓组
    {"Drum Kit", "percussion", 38, -27.0, 1.5, "Fletcher & Rossing (1998) ch. 18; membrane/plate transient balance in orchestra"},
    {"Timpani", "percussion", 43, -29.0, 1.5, "Meyer (2009) ch. 7; kettle drum orchestral balance"},
    {"Glockenspiel", "percussion", 84, -32.0, 1.0, "Meyer (2009) ch. 7; high piercing harmonics"},
    {"Vibraphone", "percussion", 60, -31.0, 1.0, "Meyer (2009) ch. 7; aluminum bar acoustic power"},
    {"Marimba", "percussion", 60, -31.0, 1.0, "Meyer (2009) ch. 7; rosewood bar resonator"},
    {"Xylophone", "percussion", 72, -30.0, 1.0, "Meyer (2009) ch. 7; hard mallet bright transient"},
    {"Tubular Bells", "percussion", 60, -30.0, 1.5, "Meyer (2009) ch. 7; chimes resonance"},
    {"Agogos", "percussion", 67, -31.0, 1.5, "Meyer (2009) ch. 7; bell metal strike"},
    {"Wood Blocks", "percussion", 76, -31.0, 1.5, "Meyer (2009) ch. 7; wood transient"},
    {"Taikos", "percussion", 60, -28.0, 1.5, "Fletcher & Rossing (1998) ch. 18; large barrel drum"},
    {"Toms", "percussion", 45, -28.0, 1.5, "Fletcher & Rossing (1998) ch. 18; tuned drum membrane"},
    {"Jewellery Music Box", "keys", 72, -33.0, 1.0, "Steel comb delicate acoustic power"},
    {"Joana's Music Box", "keys", 72, -33.0, 1.0, "Steel comb delicate acoustic power"},

    // 合唱
    {"Full Choir", "choir", 60, -28.0, 1.5, "Meyer (2009) ch. 8; 30-40 singer choral power summation"},

    // 吉他与电声
    {"Acoustic Nylon", "guitars", 60, -33.0, 1.0, "Fletcher & Rossing (1998) ch. 9; classical guitar"},
    {"Acoustic Steel Picked", "guitars", 60, -32.0, 1.0, "Fletcher & Rossing (1998) ch. 9; steel string acoustic"},
    {"Acoustic Steel Plucked", "guitars", 60, -32.0, 1.0, "Fletcher & Rossing (1998) ch. 9; steel string acoustic"},
    {"Electric SC - Clean", "guitars", 57, -30.0, 1.5, "Amplified electric guitar orchestral blend"},
    {"Electric LP - Clean", "guitars", 57, -30.0, 1.5, "Amplified electric guitar orchestral blend"},
    {"Electric LP - Heavy", "guitars", 57, -29.0, 1.5, "Compressed heavy electric guitar"},
    {"Electric Bass", "guitars", 38, -29.0, 1.5, "Rhythm section bass guitar anchor"},

    // 回退预设 (FluidSynth)
    {"Accordion", "keys", 60, -32.0, 1.0, "Meyer (2009) ch. 4; free-reed aerophone"},
    {"Electric Piano", "keys", 60, -30.0, 1.5, "DX7/Rhodes electric piano mix level"},
    {"DX7 Piano", "keys", 60, -30.0, 1.5, "DX7 FM synthesis keyboard level"},
    {"FluidSynth Drums", "percussion", 38, -27.0, 1.5, "GM drum kit rhythm balance"},
};

const InstrumentTargetDef* find_target_def(std::string_view name) {
  for (const auto& def : kTargetDefs) {
    if (name == def.name) return &def;
  }
  return nullptr;
}


// 测量 Muse 采样器单个乐器在指定力度的电平
double measure_muse_at_velocity(const MuseLib& lib, int instrument_id, const std::string& name,
                                const std::string& family, int pitch, double velocity) {
  ChannelPlan plan;
  plan.channel = 0;
  plan.instrument_id = instrument_id;
  plan.instrument_name = name;
  plan.family = family;

  // 标准测试乐句: 2 个音符, 每个 700 ms, 间隔 200 ms
  abi::ms_NoteEvent_5 n1{};
  n1._voice = 0;
  n1._location_us = 100'000;
  n1._duration_us = 700'000;
  n1._pitch = pitch;
  n1._tempo = 120.0;

  abi::ms_NoteEvent_5 n2{};
  n2._voice = 0;
  n2._location_us = 900'000;
  n2._duration_us = 700'000;
  n2._pitch = pitch;
  n2._tempo = 120.0;

  plan.notes = {n1, n2};
  plan.dynamics = {{0, velocity / 127.0}};

  SamplerSession session(lib.api());
  session.add_score({plan});
  const std::size_t total_frames = kSampleRate * 2;  // 2 秒
  const Audio audio = session.render(total_frames);

  MixInput input;
  input.channel = 1;
  input.family = family;
  input.left = audio.channels[0];
  input.right = audio.channels[1];

  return active_rms_dbfs(input, plan);
}

// 在隔离子进程中测量 MuseSampler, 防止偶发的库内段错误或 terminate 中断整个标定任务
bool measure_muse_isolated(const MuseLib& lib, int instrument_id, const std::string& name,
                           const std::string& family, int pitch,
                           double& l_mf, double& l_soft, double& l_loud) {
  int fds[2];
  if (pipe(fds) != 0) return false;

  pid_t pid = fork();
  if (pid < 0) {
    close(fds[0]);
    close(fds[1]);
    return false;
  }

  if (pid == 0) {
    close(fds[0]);
    try {
      double m_mf = measure_muse_at_velocity(lib, instrument_id, name, family, pitch, 80.0);
      double m_soft = measure_muse_at_velocity(lib, instrument_id, name, family, pitch, 50.0);
      double m_loud = measure_muse_at_velocity(lib, instrument_id, name, family, pitch, 100.0);
      double buf[3] = {m_mf, m_soft, m_loud};
      ssize_t written = write(fds[1], buf, sizeof(buf));
      (void)written;
      close(fds[1]);
      _exit(0);
    } catch (...) {
      close(fds[1]);
      _exit(1);
    }
  }

  close(fds[1]);
  double buf[3] = {0.0, 0.0, 0.0};
  ssize_t read_bytes = read(fds[0], buf, sizeof(buf));
  close(fds[0]);

  int status = 0;
  waitpid(pid, &status, 0);

  if (WIFEXITED(status) && WEXITSTATUS(status) == 0 && read_bytes == static_cast<ssize_t>(sizeof(buf))) {
    l_mf = buf[0];
    l_soft = buf[1];
    l_loud = buf[2];
    return true;
  }
  return false;
}


// 测量 FluidSynth 回退在指定力度的电平
double measure_fluidsynth_at_velocity(int gm_program, bool is_drum, const SoundfontInfo& font,
                                      int pitch, int velocity) {
  MidiSequence seq;
  seq.duration_s = 2.0;
  const auto u8 = [](int v) { return static_cast<std::uint8_t>(v); };
  const int ch = is_drum ? 9 : 0;
  seq.events.push_back({0.0, u8(0xC0 | ch), u8(gm_program), 0, ch});
  seq.events.push_back({0.1, u8(0x90 | ch), u8(pitch), u8(velocity), ch});
  seq.events.push_back({0.8, u8(0x80 | ch), u8(pitch), 0, ch});
  seq.events.push_back({0.9, u8(0x90 | ch), u8(pitch), u8(velocity), ch});
  seq.events.push_back({1.6, u8(0x80 | ch), u8(pitch), 0, ch});
  RenderOptions options;
  options.reverb.level = 0.0;
  const std::size_t total_frames = kSampleRate * 2;
  const Audio audio = render_fluidsynth_channel(seq, ch, font, options, total_frames);

  ChannelPlan plan;
  abi::ms_NoteEvent_5 n1{};
  n1._location_us = 100'000;
  n1._duration_us = 700'000;
  abi::ms_NoteEvent_5 n2{};
  n2._location_us = 900'000;
  n2._duration_us = 700'000;
  plan.notes = {n1, n2};

  MixInput input;
  input.channel = ch + 1;
  input.left = audio.channels[0];
  input.right = audio.channels[1];

  return active_rms_dbfs(input, plan);
}

}  // namespace

std::vector<LevelCalibration> run_calibration(const CalibrateOptions& options) {
  std::vector<LevelCalibration> results;

  const std::string samples = options.samples_dir.empty()
                                  ? env_or("MUSE_SAMPLES_DIR", kDefaultSamples)
                                  : options.samples_dir;
  const Catalog full_catalog = load_catalog(samples);
  if (full_catalog.empty()) {
    throw Error("muse_unavailable", "Muse 样本目录为空或未找到: " + samples);
  }

  const std::string lib_path = options.library_path.empty()
                                   ? env_or("MUSESAMPLER_LIB", kDefaultLibrary)
                                   : options.library_path;
  const MuseLib lib = MuseLib::load(lib_path);
  lib.init(samples);
  const std::vector<InstrumentEntry> accepted = lib.list_instruments();
  const Catalog catalog = intersect_catalog(full_catalog, accepted);

  // 1. 标定 Muse 乐器
  for (const auto& target_def : kTargetDefs) {
    if (std::string_view(target_def.name).find("FluidSynth") != std::string_view::npos ||
        std::string_view(target_def.name) == "Accordion" ||
        std::string_view(target_def.name) == "Electric Piano" ||
        std::string_view(target_def.name) == "Electric Bass" ||
        std::string_view(target_def.name) == "DX7 Piano") {
      continue;  // 回退乐器在后面处理
    }
    const MuseInstrument* found = nullptr;
    for (const auto& entry : catalog) {
      if (entry.name == target_def.name) {
        found = &entry;
        break;
      }
    }
    if (found == nullptr) {
      std::fprintf(stderr, "WARN: Instrument '%s' not installed in catalog, skipping.\n", target_def.name);
      continue;
    }

    std::fprintf(stderr, "Calibrating [%s] id=%d... ", target_def.name, found->id);
    double l_mf = 0.0, l_soft = 0.0, l_loud = 0.0;
    if (measure_muse_isolated(lib, found->id, target_def.name,
                              target_def.family, target_def.pitch,
                              l_mf, l_soft, l_loud)) {
      double slope = (l_loud - l_soft) / 50.0;
      if (!std::isfinite(slope) || slope < 0.05) slope = 0.20;

      LevelCalibration cal;
      cal.name = target_def.name;
      cal.family = target_def.family;
      cal.measured_mf = l_mf;
      cal.target_mf = target_def.target_mf;
      cal.gain_db = target_def.target_mf - l_mf;
      cal.slope = slope;
      cal.uncertainty_db = target_def.uncertainty_db;
      cal.source = target_def.source;
      results.push_back(std::move(cal));

      std::fprintf(stderr, "L_mf=%.2f target=%.2f gain=%.2f slope=%.3f\n",
                   l_mf, target_def.target_mf, target_def.target_mf - l_mf, slope);
    } else {
      std::fprintf(stderr, "FAILED (isolated process crash/error, skipped)\n");
    }
  }

  // 2. 标定 FluidSynth 回退乐器
  try {
    const fs::path sf_dir = options.soundfont_dir.empty() ? soundfont_dir() : fs::path(options.soundfont_dir);
    const SoundfontInfo font = default_soundfont(sf_dir);

    struct FallbackItem {
      const char* name;
      int program;
      bool is_drum;
      int pitch;
    };
    const FallbackItem fallbacks[] = {
        {"Accordion", 21, false, 60},
        {"Electric Piano", 4, false, 60},
        {"DX7 Piano", 5, false, 60},
        {"Electric Bass", 33, false, 38},
        {"FluidSynth Drums", 0, true, 38},
    };

    for (const auto& item : fallbacks) {
      const InstrumentTargetDef* def = find_target_def(item.name);
      if (!def) continue;

      std::fprintf(stderr, "Calibrating FluidSynth [%s]... ", item.name);
      const double l_mf = measure_fluidsynth_at_velocity(item.program, item.is_drum, font, item.pitch, 80);
      const double l_soft = measure_fluidsynth_at_velocity(item.program, item.is_drum, font, item.pitch, 50);
      const double l_loud = measure_fluidsynth_at_velocity(item.program, item.is_drum, font, item.pitch, 100);

      double slope = (l_loud - l_soft) / 50.0;
      if (!std::isfinite(slope) || slope < 0.05) slope = 0.20;

      LevelCalibration cal;
      cal.name = def->name;
      cal.family = def->family;
      cal.measured_mf = l_mf;
      cal.target_mf = def->target_mf;
      cal.gain_db = def->target_mf - l_mf;
      cal.slope = slope;
      cal.uncertainty_db = def->uncertainty_db;
      cal.source = def->source;
      results.push_back(std::move(cal));

      std::fprintf(stderr, "L_mf=%.2f target=%.2f gain=%.2f slope=%.3f\n",
                   l_mf, def->target_mf, def->target_mf - l_mf, slope);
    }
  } catch (const std::exception& ex) {
    std::fprintf(stderr, "FluidSynth calibration skipped: %s\n", ex.what());
  }

  return results;
}

std::string generate_levels_cpp(const std::vector<LevelCalibration>& cal) {
  std::ostringstream out;
  const auto now = std::chrono::system_clock::to_time_t(std::chrono::system_clock::now());
  struct tm tm_buf{};
  gmtime_r(&now, &tm_buf);

  out << "// Generated by cgmusic calibrate --engine muse on "
      << std::put_time(&tm_buf, "%Y-%m-%d %H:%M:%S UTC") << "\n"
      << "// Target levels grounded in:\n"
      << "//   1. J. Meyer, Acoustics and the Performance of Music (5th ed., Springer 2009)\n"
      << "//   2. N. H. Fletcher & T. D. Rossing, The Physics of Musical Instruments (2nd ed., Springer 1998)\n"
      << "//   3. Orchestral balance conventions & incoherent power summation (10 log10 N)\n"
      << "#include \"muse/levels.hpp\"\n\n"
      << "#include <algorithm>\n"
      << "#include <cctype>\n"
      << "#include <string_view>\n\n"
      << "namespace cgmusic::muse {\n"
      << "namespace {\n\n"
      << "std::string lower_copy(std::string_view s) {\n"
      << "  std::string out(s);\n"
      << "  for (char& c : out) {\n"
      << "    if (c >= 'A' && c <= 'Z') c = static_cast<char>(c - 'A' + 'a');\n"
      << "  }\n"
      << "  return out;\n"
      << "}\n\n"
      << "const std::vector<LevelCalibration> kCalibratedTable = {\n";

  for (const auto& item : cal) {
    out << "    {\"" << item.name << "\", \"" << item.family << "\", "
        << std::fixed << std::setprecision(4) << item.measured_mf << ", "
        << item.target_mf << ", "
        << item.gain_db << ", "
        << item.slope << ", "
        << std::setprecision(1) << item.uncertainty_db << ", \""
        << item.source << "\"},\n";
  }

  out << "};\n\n"
      << "const LevelCalibration kDefaultCalibration = {\n"
      << "    \"default\", \"default\", -31.0, -31.0, 0.0, 0.20, 1.5, \"Generic orchestral fallback\"\n"
      << "};\n\n"
      << "const LevelCalibration kFamilyDefaults[] = {\n"
      << "    {\"woodwinds\", \"woodwinds\", -33.0, -33.0, 0.0, 0.20, 1.0, \"Meyer (2009) woodwinds anchor\"},\n"
      << "    {\"brass\", \"brass\", -31.0, -31.0, 0.0, 0.20, 1.0, \"Meyer (2009) brass anchor\"},\n"
      << "    {\"strings\", \"strings\", -29.0, -29.0, 0.0, 0.20, 1.5, \"Meyer (2009) strings section balance\"},\n"
      << "    {\"keys\", \"keys\", -29.0, -29.0, 0.0, 0.20, 1.5, \"Fletcher & Rossing (1998) piano balance\"},\n"
      << "    {\"percussion\", \"percussion\", -27.0, -27.0, 0.0, 0.20, 1.5, \"Fletcher & Rossing (1998) percussion balance\"},\n"
      << "    {\"choir\", \"choir\", -28.0, -28.0, 0.0, 0.20, 1.5, \"Meyer (2009) choral power summation\"},\n"
      << "    {\"guitars\", \"guitars\", -31.0, -31.0, 0.0, 0.20, 1.5, \"Acoustic/electric guitars\"},\n"
      << "};\n\n"
      << "}  // namespace\n\n"
      << "const std::vector<LevelCalibration>& calibration_table() {\n"
      << "  return kCalibratedTable;\n"
      << "}\n\n"
      << "const LevelCalibration& find_calibration(std::string_view instrument_name,\n"
      << "                                         std::string_view family,\n"
      << "                                         int program) {\n"
      << "  const std::string lowered_name = lower_copy(instrument_name);\n"
      << "  const std::string lowered_fam = lower_copy(family);\n\n"
      << "  // 1. 精确名称或别名匹配\n"
      << "  for (const auto& item : kCalibratedTable) {\n"
      << "    if (lower_copy(item.name) == lowered_name) return item;\n"
      << "  }\n"
      << "  // 特殊声部名子串匹配\n"
      << "  for (const auto& item : kCalibratedTable) {\n"
      << "    const std::string key = lower_copy(item.name);\n"
      << "    if (!lowered_name.empty() && (lowered_name.find(key) != std::string::npos || key.find(lowered_name) != std::string::npos)) {\n"
      << "      return item;\n"
      << "    }\n"
      << "  }\n"
      << "  // 2. GM 回退程序匹配\n"
      << "  if (program == 21 || lowered_name.find(\"accordion\") != std::string::npos) {\n"
      << "    for (const auto& item : kCalibratedTable) if (item.name == \"Accordion\") return item;\n"
      << "  }\n"
      << "  if (program == 4 || program == 5) {\n"
      << "    for (const auto& item : kCalibratedTable) if (item.name == \"Electric Piano\") return item;\n"
      << "  }\n"
      << "  // 3. 家族默认\n"
      << "  for (const auto& def : kFamilyDefaults) {\n"
      << "    if (def.family == lowered_fam) return def;\n"
      << "  }\n"
      << "  return kDefaultCalibration;\n"
      << "}\n\n"
      << "}  // namespace cgmusic::muse\n";

  return out.str();
}

std::string format_calibration_json(const std::vector<LevelCalibration>& cal) {
  std::ostringstream out;
  out << "{\"instruments\":[";
  for (std::size_t i = 0; i < cal.size(); ++i) {
    const auto& c = cal[i];
    if (i != 0) out << ",";
    out << "{\"name\":\"" << c.name << "\""
        << ",\"family\":\"" << c.family << "\""
        << ",\"measured_mf\":" << c.measured_mf
        << ",\"target_mf\":" << c.target_mf
        << ",\"gain_db\":" << c.gain_db
        << ",\"slope\":" << c.slope
        << ",\"uncertainty_db\":" << c.uncertainty_db
        << ",\"source\":\"" << c.source << "\"}";
  }
  out << "]}\n";
  return out.str();
}

}  // namespace cgmusic::muse
