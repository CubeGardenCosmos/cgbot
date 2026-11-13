#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <iostream>
#include <optional>
#include <set>
#include <sstream>
#include <string>
#include <vector>

#include "common.hpp"
#include "encode.hpp"
#include "loudness.hpp"
#include "midi.hpp"
#include "musicxml.hpp"
#include "render.hpp"
#include "soundfont.hpp"

namespace fs = std::filesystem;

namespace cgmusic {
namespace {

constexpr double kTargetLufs = -14.0;
constexpr double kCeilingDbtp = -1.5;
constexpr double kMaxGainDb = 12.0;
constexpr double kFadeSeconds = 0.5;
constexpr const char* kOutputDir = "/workspace/artifacts/music";

// 非有限值输出为 null,避免生成非法 JSON。
std::string number(double value) {
  if (!std::isfinite(value)) return "null";
  std::ostringstream out;
  out.precision(9);
  out << value;
  return out.str();
}

// 字体文件格式:扩展名小写且不含点(sf2 / sf3)。
std::string font_format(const fs::path& path) {
  std::string ext = path.extension().string();
  std::transform(ext.begin(), ext.end(), ext.begin(),
                 [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
  return ext.empty() ? std::string() : ext.substr(1);
}

struct RenderArgs {
  std::optional<std::string> input;
  std::optional<std::string> engine;
  std::optional<std::string> soundfont;
  std::optional<std::string> fmt;
  std::optional<std::string> output;
  std::optional<std::string> gain;
  std::vector<std::string> reverb;
  std::vector<std::string> font_for_channel;
  std::optional<std::string> reverb_room;
  std::optional<std::string> reverb_level;
};

RenderArgs parse_render_args(int argc, char** argv) {
  RenderArgs args;
  for (int i = 2; i < argc; ++i) {
    const std::string arg = argv[i];
    const auto value = [&]() -> std::string {
      if (i + 1 >= argc) throw Error("bad_input", "Missing value for " + arg);
      return argv[++i];
    };
    if (arg == "--engine") {
      args.engine = value();
    } else if (arg == "--soundfont") {
      args.soundfont = value();
    } else if (arg == "--fmt") {
      args.fmt = value();
    } else if (arg == "-o" || arg == "--output") {
      args.output = value();
    } else if (arg == "--gain") {
      args.gain = value();
    } else if (arg == "--reverb") {
      args.reverb.push_back(value());
    } else if (arg == "--font-for-channel") {
      args.font_for_channel.push_back(value());
    } else if (arg == "--reverb-room") {
      args.reverb_room = value();
    } else if (arg == "--reverb-level") {
      args.reverb_level = value();
    } else if (!arg.empty() && arg[0] == '-') {
      throw Error("bad_input", "Unknown option " + arg);
    } else if (args.input) {
      throw Error("bad_input", "Only one input file is accepted");
    } else {
      args.input = arg;
    }
  }
  if (!args.input) {
    throw Error("bad_input",
                "usage: cgmusic render <file> [--engine fluidsynth|muse] [--soundfont ID|FILE] [--fmt mp3|wav|flac] "
                "[-o OUT] [--gain G] [--reverb CH=0-127]... [--font-for-channel CH=ID]... "
                "[--reverb-room R] [--reverb-level L]");
  }
  return args;
}

// 解析 [min, max] 内的有限数值;整串必须全部被消费。
double parse_number(const std::string& text, double min, double max, const char* name) {
  std::size_t used = 0;
  double value = 0.0;
  try {
    value = std::stod(text, &used);
  } catch (const std::exception&) {
    throw Error("bad_input", std::string("Invalid ") + name);
  }
  if (used != text.size() || !std::isfinite(value) || value < min || value > max) {
    throw Error("bad_input", std::string(name) + " is out of range");
  }
  return value;
}

int parse_int(const std::string& text, int min, int max, const char* name) {
  const double value = parse_number(text, min, max, name);
  if (value != std::floor(value)) {
    throw Error("bad_input", std::string(name) + " must be an integer");
  }
  return static_cast<int>(value);
}

// --gain:相对 -14 LUFS 目标的 dB 偏移(有限且在 ±12 dB 内)。
double parse_gain(const std::optional<std::string>& text) {
  if (!text) return 0.0;
  return parse_number(*text, -kMaxGainDb, kMaxGainDb, "--gain");
}

// --reverb CH=V:MIDI 通道 1-16 的混响发送(CC91,0-127),同一通道只能指定一次。
ReverbOptions parse_reverb_options(const RenderArgs& args) {
  ReverbOptions reverb;
  for (const std::string& text : args.reverb) {
    const auto eq = text.find('=');
    if (eq == std::string::npos) throw Error("bad_input", "--reverb expects CH=0-127");
    const int channel = parse_int(text.substr(0, eq), 1, 16, "--reverb channel");
    const int send = parse_int(text.substr(eq + 1), 0, 127, "--reverb send");
    auto& slot = reverb.channel_send[static_cast<std::size_t>(channel - 1)];
    if (slot) {
      throw Error("bad_input", "Duplicate --reverb for channel " + std::to_string(channel));
    }
    slot = send;
  }
  if (args.reverb_room) {
    reverb.room = parse_number(*args.reverb_room, 0.0, 1.0, "--reverb-room");
  }
  if (args.reverb_level) {
    reverb.level = parse_number(*args.reverb_level, 0.0, 1.0, "--reverb-level");
  }
  return reverb;
}

// --font-for-channel CH=ID:把 MIDI 通道 1-16 绑定到指定字体(ID 或 .sf2/.sf3 文件路径),同一通道只能指定一次。
std::vector<FontOverride> parse_font_overrides(const RenderArgs& args, const fs::path& dir) {
  std::vector<FontOverride> overrides;
  std::set<int> seen;
  for (const std::string& text : args.font_for_channel) {
    const auto eq = text.find('=');
    if (eq == std::string::npos) throw Error("bad_input", "--font-for-channel expects CH=ID");
    const int channel = parse_int(text.substr(0, eq), 1, 16, "--font-for-channel channel");
    const std::string spec = text.substr(eq + 1);
    if (spec.empty()) throw Error("bad_input", "--font-for-channel expects CH=ID");
    if (!seen.insert(channel).second) {
      throw Error("bad_input", "Duplicate --font-for-channel for channel " + std::to_string(channel));
    }
    overrides.push_back({channel - 1, resolve_soundfont(dir, spec)});
  }
  return overrides;
}

void print_missing(const MissingPreset& miss) {
  std::cout << "{\"channel\":" << miss.channel << ",\"bank\":" << miss.wanted.bank
            << ",\"program\":" << miss.wanted.program
            << ",\"reason\":" << json_string(miss.reason) << "}";
}

int cmd_render(int argc, char** argv) {
  const RenderArgs args = parse_render_args(argc, argv);
  const fs::path input = *args.input;
  const RenderEngine engine = parse_render_engine(args.engine.value_or("fluidsynth"));
  const AudioFormat format = parse_format(args.fmt.value_or("mp3"));
  const double gain_db = parse_gain(args.gain);

  RenderOptions options;
  // 声部平衡固定开启;CGMUSIC_BALANCE=off 仅供开发测试,不在 CLI 与 SKILL.md 中公开。
  const char* balance_env = std::getenv("CGMUSIC_BALANCE");
  options.balance = balance_env == nullptr || std::string(balance_env) != "off";
  options.reverb = parse_reverb_options(args);

  const std::vector<std::uint8_t> data = read_file(input, kMaxInputBytes);
  std::vector<std::uint8_t> midi;
  if (data.size() >= 4 && std::memcmp(data.data(), "MThd", 4) == 0) {
    midi = data;
  } else {
    midi = musicxml_to_midi(data, input.filename().string());
  }
  const MidiSequence sequence = parse_midi(midi);

  RenderReport report;
  MuseRenderResult muse_report;
  std::optional<SoundfontInfo> font;
  Audio audio;
  if (engine == RenderEngine::Muse) {
    if (args.soundfont || !args.font_for_channel.empty()) {
      throw Error("bad_input", "--soundfont 与 --font-for-channel 仅适用于 fluidsynth 引擎");
    }
    audio = render_sequence_muse(sequence, options, kTargetLufs + gain_db, &muse_report);
  } else {
    const fs::path dir = soundfont_dir();
    font = args.soundfont ? resolve_soundfont(dir, *args.soundfont) : default_soundfont(dir);
    options.font_overrides = parse_font_overrides(args, dir);
    audio = render_sequence(sequence, *font, options, &report);
  }
  trim_silence(audio);
  fade_tail(audio, kFadeSeconds);
  // Muse 的混音器已经完成同一目标的响度与真峰值处理;编码与后续报告共用。
  const Quality quality = engine == RenderEngine::Muse
                              ? measure(audio)
                              : master(audio, kTargetLufs + gain_db, kCeilingDbtp);

  fs::path output;
  if (args.output) {
    output = *args.output;
  } else {
    output = fs::path(kOutputDir) / (input.stem().string() + "." + std::string(format_name(format)));
  }
  if (output.has_parent_path()) fs::create_directories(output.parent_path());
  encode_audio(audio, format, output);

  std::cout << "{\"path\":" << json_string(fs::absolute(output).string())
            << ",\"fmt\":" << json_string(format_name(format))
            << ",\"engine\":" << json_string(render_engine_name(engine))
            << ",\"duration_s\":" << number(quality.duration_s)
            << ",\"midi_seconds\":" << number(sequence.duration_s)
            << ",\"soundfont\":" << (font ? json_string(font->id) : "null")
            << ",\"lufs\":" << number(quality.lufs)
            << ",\"true_peak_dbtp\":" << number(quality.true_peak_dbtp)
            << ",\"lra\":" << number(quality.lra)
            << ",\"clipped\":" << (quality.clipped ? "true" : "false")
            << ",\"balance\":[";
  for (std::size_t i = 0; i < report.stems.size(); ++i) {
    const AppliedStem& stem = report.stems[i];
    if (i != 0) std::cout << ",";
    std::cout << "{\"stem\":" << json_string(stem.name)
              << ",\"offset_db\":" << number(stem.offset_db)
              << ",\"gain_db\":" << number(stem.gain_db) << "}";
  }
  std::cout << "],\"reverb\":{\"room\":" << number(report.reverb_room)
            << ",\"level\":" << number(report.reverb_level) << ",\"channels\":[";
  for (std::size_t i = 0; i < report.channels.size(); ++i) {
    const ChannelReport& channel = report.channels[i];
    if (i != 0) std::cout << ",";
    std::cout << "{\"ch\":" << channel.channel << ",\"program\":" << channel.program
              << ",\"reverb_send\":" << channel.reverb
              << ",\"reverb_source\":" << json_string(channel.reverb_source)
              << ",\"chorus_send\":" << channel.chorus
              << ",\"chorus_source\":" << json_string(channel.chorus_source) << "}";
  }
  std::cout << "]},\"fonts\":[";
  for (std::size_t f = 0; f < report.fonts.size(); ++f) {
    const FontReport& item = report.fonts[f];
    if (f != 0) std::cout << ",";
    std::cout << "{\"id\":" << json_string(item.id) << ",\"file\":" << json_string(item.file)
              << ",\"channels\":[";
    for (std::size_t i = 0; i < item.channels.size(); ++i) {
      if (i != 0) std::cout << ",";
      std::cout << item.channels[i];
    }
    std::cout << "],\"missing_presets\":[";
    for (std::size_t i = 0; i < item.missing_presets.size(); ++i) {
      if (i != 0) std::cout << ",";
      print_missing(item.missing_presets[i]);
    }
    std::cout << "]}";
  }
  std::cout << "],\"fallbacks\":[";
  for (std::size_t i = 0; i < report.fallbacks.size(); ++i) {
    if (i != 0) std::cout << ",";
    print_missing(report.fallbacks[i]);
  }
  std::cout << "]";
  if (engine == RenderEngine::Muse) {
    std::cout << ",\"muse\":{\"version\":" << json_string(muse_report.version)
              << ",\"dry_seconds\":" << number(muse_report.dry_seconds) << ",\"tracks\":[";
    for (std::size_t i = 0; i < muse_report.tracks.size(); ++i) {
      const MuseTrackReport& track = muse_report.tracks[i];
      if (i != 0) std::cout << ",";
      std::cout << "{\"channel\":" << track.channel << ",\"program\":" << track.program
                << ",\"instrument_id\":" << track.instrument_id
                << ",\"instrument\":" << json_string(track.instrument)
                << ",\"family\":" << json_string(track.family)
                << ",\"notes\":" << track.note_count << "}";
    }
    std::cout << "],\"mix\":" << muse_report.mix_report << "}";
  }
  std::cout << "}\n";
  return 0;
}

int cmd_soundfonts() {
  const fs::path dir = soundfont_dir();
  const auto fonts = scan_soundfonts(dir);
  std::cout << "{\"dir\":" << json_string(dir.string()) << ",\"fonts\":[";
  for (std::size_t i = 0; i < fonts.size(); ++i) {
    const auto& font = fonts[i];
    if (i != 0) std::cout << ",";
    const std::size_t preset_count = list_font_presets(font.path).size();
    std::cout << "{\"id\":" << json_string(font.id)
              << ",\"file\":" << json_string(font.file_name)
              << ",\"bytes\":" << font.bytes
              << ",\"default\":" << (font.is_default ? "true" : "false")
              << ",\"format\":" << json_string(font_format(font.path))
              << ",\"preset_count\":" << preset_count << "}";
  }
  std::cout << "]}\n";
  return 0;
}

int cmd_presets(int argc, char** argv) {
  std::optional<std::string> spec;
  for (int i = 2; i < argc; ++i) {
    const std::string arg = argv[i];
    if (arg != "--soundfont" || i + 1 >= argc) {
      throw Error("bad_input", "usage: cgmusic presets [--soundfont ID|FILE]");
    }
    spec = argv[++i];
  }
  const fs::path dir = soundfont_dir();
  const SoundfontInfo font = spec ? resolve_soundfont(dir, *spec) : default_soundfont(dir);
  const auto presets = list_font_presets(font.path);
  std::cout << "{\"soundfont\":" << json_string(font.id)
            << ",\"file\":" << json_string(font.file_name) << ",\"presets\":[";
  for (std::size_t i = 0; i < presets.size(); ++i) {
    const PresetInfo& preset = presets[i];
    if (i != 0) std::cout << ",";
    std::cout << "{\"bank\":" << preset.bank << ",\"program\":" << preset.program
              << ",\"name\":" << json_string(preset.name) << "}";
  }
  std::cout << "]}\n";
  return 0;
}

int cmd_analyze(int argc, char** argv) {
  if (argc < 3) throw Error("bad_input", "usage: cgmusic analyze <audio>");
  const Audio audio = read_audio(argv[2]);
  const Quality quality = measure(audio);
  std::cout << "{\"lufs\":" << number(quality.lufs)
            << ",\"true_peak_dbtp\":" << number(quality.true_peak_dbtp)
            << ",\"lra\":" << number(quality.lra)
            << ",\"clipped\":" << (quality.clipped ? "true" : "false")
            << ",\"duration_s\":" << number(quality.duration_s) << "}\n";
  return 0;
}

int run(int argc, char** argv) {
  if (argc < 2) throw Error("bad_input", "usage: cgmusic render|presets|soundfonts|analyze ...");
  const std::string command = argv[1];
  if (command == "render") return cmd_render(argc, argv);
  if (command == "presets") return cmd_presets(argc, argv);
  if (command == "soundfonts") return cmd_soundfonts();
  if (command == "analyze") return cmd_analyze(argc, argv);
  throw Error("unsupported", "Unknown command: " + command);
}

void print_error(const std::string& code, const std::string& message) {
  std::cerr << "{\"error\":" << json_string(code) << ",\"message\":" << json_string(message)
            << "}\n";
}

}  // namespace
}  // namespace cgmusic

int main(int argc, char** argv) {
  try {
    return cgmusic::run(argc, argv);
  } catch (const cgmusic::Error& error) {
    cgmusic::print_error(error.code(), error.what());
  } catch (const std::exception& error) {
    cgmusic::print_error("synth_failed", error.what());
  }
  return 1;
}
