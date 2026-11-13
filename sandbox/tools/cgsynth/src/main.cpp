#include <cmath>
#include <cstddef>
#include <filesystem>
#include <iostream>
#include <optional>
#include <set>
#include <string>
#include <vector>

#include "common.hpp"
#include "midi.hpp"
#include "render.hpp"
#include "soundfont.hpp"
#include "wav.hpp"

namespace fs = std::filesystem;
namespace cgmusic {
namespace {

constexpr std::size_t kTailFrames = 4 * kSampleRate;

std::string number(double value) {
  if (!std::isfinite(value)) return "null";
  std::string text = std::to_string(value);
  while (text.size() > 1 && text.back() == '0') text.pop_back();
  if (!text.empty() && text.back() == '.') text.push_back('0');
  return text;
}

std::string font_format(const fs::path& path) {
  std::string ext = path.extension().string();
  for (char& c : ext) {
    if (c >= 'A' && c <= 'Z') c = static_cast<char>(c - 'A' + 'a');
  }
  return ext.empty() ? std::string() : ext.substr(1);
}

void print_missing(const MissingPreset& miss) {
  std::cout << "{\"channel\":" << miss.channel << ",\"bank\":" << miss.wanted.bank
            << ",\"program\":" << miss.wanted.program
            << ",\"reason\":" << json_string(miss.reason) << "}";
}

struct RenderArgs {
  fs::path input;
  fs::path output;
  RenderEngine engine = RenderEngine::FluidSynth;
  std::optional<std::string> soundfont;
};

RenderArgs parse_render_args(int argc, char** argv) {
  RenderArgs args;
  bool have_input = false;
  bool have_output = false;
  bool have_engine = false;
  for (int i = 2; i < argc; ++i) {
    const std::string arg = argv[i];
    const auto value = [&]() -> std::string {
      if (i + 1 >= argc) throw Error("bad_input", "Missing value for " + arg);
      return argv[++i];
    };
    if (arg == "--engine" && !have_engine) {
      args.engine = parse_render_engine(value());
      have_engine = true;
    } else if (arg == "--soundfont" && !args.soundfont) {
      args.soundfont = value();
    } else if (arg == "-o" && !have_output) {
      args.output = value();
      have_output = true;
    } else if (!arg.empty() && arg.front() == '-') {
      throw Error("bad_input", "Unknown or duplicate option " + arg);
    } else if (have_input) {
      throw Error("bad_input", "Only one input file is accepted");
    } else {
      args.input = arg;
      have_input = true;
    }
  }
  if (!have_input || !have_output) {
    throw Error("bad_input",
                "usage: cgsynth render <part.mid> --engine fluidsynth|muse [--soundfont ID|FILE] -o <stem.wav>");
  }
  if (args.engine == RenderEngine::Muse && args.soundfont) {
    throw Error("bad_input", "--soundfont is only valid with --engine fluidsynth");
  }
  return args;
}

int first_sounding_channel(const MidiSequence& sequence) {
  std::set<int> channels;
  for (const MidiEvent& event : sequence.events) {
    if ((event.status & 0xF0) == 0x90 && event.data2 != 0) channels.insert(event.channel);
  }
  if (channels.empty()) throw Error("bad_input", "MIDI file contains no notes");
  if (channels.size() != 1) throw Error("bad_input", "cgsynth render expects a single-part MIDI file");
  return *channels.begin();
}

int cmd_render(int argc, char** argv) {
  const RenderArgs args = parse_render_args(argc, argv);
  const MidiSequence sequence = parse_midi(read_file(args.input, kMaxInputBytes));
  const int channel = first_sounding_channel(sequence);
  const std::size_t frames = static_cast<std::size_t>(std::ceil(sequence.duration_s * kSampleRate)) +
                             kTailFrames;

  Audio audio;
  RenderReport report;
  MuseRenderResult muse_report;
  std::optional<SoundfontInfo> font;
  if (args.engine == RenderEngine::FluidSynth) {
    const fs::path dir = soundfont_dir();
    font = args.soundfont ? resolve_soundfont(dir, *args.soundfont) : default_soundfont(dir);
    audio = render_fluidsynth_channel(sequence, channel, *font, frames, &report);
  } else {
    audio = render_sequence_muse(sequence, &muse_report);
    report.fallbacks = muse_report.fallbacks;
  }
  if (args.output.has_parent_path()) fs::create_directories(args.output.parent_path());
  write_float_wav(audio, args.output);

  std::cout << "{\"engine\":" << json_string(render_engine_name(args.engine))
            << ",\"duration_s\":" << number(static_cast<double>(audio.frames()) / kSampleRate)
            << ",\"soundfont\":" << (font ? json_string(font->id) : "null")
            << ",\"fonts\":[";
  for (std::size_t i = 0; i < report.fonts.size(); ++i) {
    if (i) std::cout << ',';
    const FontReport& item = report.fonts[i];
    std::cout << "{\"id\":" << json_string(item.id) << ",\"file\":" << json_string(item.file)
              << ",\"missing_presets\":[";
    for (std::size_t j = 0; j < item.missing_presets.size(); ++j) {
      if (j) std::cout << ',';
      print_missing(item.missing_presets[j]);
    }
    std::cout << "]}";
  }
  std::cout << "],\"missing_presets\":[";
  bool first = true;
  for (const FontReport& item : report.fonts) {
    for (const MissingPreset& miss : item.missing_presets) {
      if (!first) std::cout << ',';
      first = false;
      print_missing(miss);
    }
  }
  std::cout << "],\"fallbacks\":[";
  for (std::size_t i = 0; i < report.fallbacks.size(); ++i) {
    if (i) std::cout << ',';
    print_missing(report.fallbacks[i]);
  }
  if (args.engine == RenderEngine::FluidSynth) {
    std::cout << "],\"muse\":null}\n";
    return 0;
  }
  std::cout << "],\"muse\":{\"version\":" << json_string(muse_report.version)
            << ",\"dry_seconds\":" << number(muse_report.dry_seconds) << ",\"tracks\":[";
  for (std::size_t i = 0; i < muse_report.tracks.size(); ++i) {
    if (i) std::cout << ',';
    const MuseTrackReport& track = muse_report.tracks[i];
    std::cout << "{\"channel\":" << track.channel << ",\"program\":" << track.program
              << ",\"instrument_id\":" << track.instrument_id
              << ",\"instrument\":" << json_string(track.instrument)
              << ",\"family\":" << json_string(track.family)
              << ",\"notes\":" << track.note_count << "}";
  }
  std::cout << "]}}\n";
  return 0;
}

int cmd_soundfonts(int argc) {
  if (argc != 2) throw Error("bad_input", "usage: cgsynth soundfonts");
  const fs::path dir = soundfont_dir();
  const auto fonts = scan_soundfonts(dir);
  std::cout << "{\"dir\":" << json_string(dir.string()) << ",\"fonts\":[";
  for (std::size_t i = 0; i < fonts.size(); ++i) {
    const auto& font = fonts[i];
    if (i) std::cout << ',';
    std::cout << "{\"id\":" << json_string(font.id)
              << ",\"file\":" << json_string(font.file_name) << ",\"bytes\":" << font.bytes
              << ",\"default\":" << (font.is_default ? "true" : "false")
              << ",\"format\":" << json_string(font_format(font.path))
              << ",\"preset_count\":" << list_font_presets(font.path).size() << "}";
  }
  std::cout << "]}\n";
  return 0;
}

int cmd_presets(int argc, char** argv) {
  if (argc != 3) throw Error("bad_input", "usage: cgsynth presets <file>");
  const fs::path file = argv[2];
  if (!fs::exists(file)) {
    const SoundfontInfo resolved = resolve_soundfont(soundfont_dir(), argv[2]);
    const auto presets = list_font_presets(resolved.path);
    std::cout << "{\"soundfont\":" << json_string(resolved.id)
              << ",\"file\":" << json_string(resolved.file_name) << ",\"presets\":[";
    for (std::size_t i = 0; i < presets.size(); ++i) {
      if (i) std::cout << ',';
      const PresetInfo& preset = presets[i];
      std::cout << "{\"bank\":" << preset.bank << ",\"program\":" << preset.program
                << ",\"name\":" << json_string(preset.name) << "}";
    }
    std::cout << "]}\n";
    return 0;
  }
  const SoundfontInfo font{"file", file.filename().string(), file, 0, false};
  const auto presets = list_font_presets(file);
  std::cout << "{\"soundfont\":" << json_string(font.id)
            << ",\"file\":" << json_string(font.file_name) << ",\"presets\":[";
  for (std::size_t i = 0; i < presets.size(); ++i) {
    if (i) std::cout << ',';
    const PresetInfo& preset = presets[i];
    std::cout << "{\"bank\":" << preset.bank << ",\"program\":" << preset.program
              << ",\"name\":" << json_string(preset.name) << "}";
  }
  std::cout << "]}\n";
  return 0;
}

int run(int argc, char** argv) {
  if (argc < 2) throw Error("bad_input", "usage: cgsynth render|presets|soundfonts ...");
  const std::string command = argv[1];
  if (command == "render") return cmd_render(argc, argv);
  if (command == "presets") return cmd_presets(argc, argv);
  if (command == "soundfonts") return cmd_soundfonts(argc);
  throw Error("unsupported", "Unknown command: " + command);
}

void print_error(const std::string& code, const std::string& message) {
  std::cerr << format_error_json(code, message) << "\n";
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
  } catch (...) {
    cgmusic::print_error("synth_failed", "Unknown unhandled exception");
  }
  return 1;
}
