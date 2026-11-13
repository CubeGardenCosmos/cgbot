#include "wav.hpp"

#include <array>
#include <cstdint>
#include <fstream>
#include <string>
#include <vector>

namespace cgmusic {
namespace {

void le16(std::ostream& out, std::uint16_t value) {
  const std::array<char, 2> bytes{static_cast<char>(value), static_cast<char>(value >> 8)};
  out.write(bytes.data(), bytes.size());
}

void le32(std::ostream& out, std::uint32_t value) {
  const std::array<char, 4> bytes{static_cast<char>(value), static_cast<char>(value >> 8),
                                  static_cast<char>(value >> 16), static_cast<char>(value >> 24)};
  out.write(bytes.data(), bytes.size());
}

}  // namespace

void write_float_wav(const Audio& audio, const std::filesystem::path& path) {
  if (audio.rate != kSampleRate || audio.channels.size() != 2 ||
      audio.channels[0].size() != audio.channels[1].size()) {
    throw Error("synth_failed", "Stem must be 44.1 kHz stereo audio");
  }
  const std::uint64_t frames = audio.frames();
  const std::uint64_t bytes = frames * 2 * sizeof(float);
  if (bytes > 0xFFFFFFC7ULL) throw Error("bad_input", "Stem exceeds the WAV size limit");
  const std::uint32_t data_size = static_cast<std::uint32_t>(bytes);

  std::ofstream out(path, std::ios::binary | std::ios::trunc);
  if (!out) throw Error("synth_failed", "Cannot open stem output: " + path.string());

  // RIFF header
  out.write("RIFF", 4);
  le32(out, 4 + (8 + 18) + (8 + 4) + (8 + data_size));
  out.write("WAVE", 4);

  // fmt chunk (WAVE_FORMAT_IEEE_FLOAT = 3, cbSize = 0)
  out.write("fmt ", 4);
  le32(out, 18);
  le16(out, 3);  // WAVE_FORMAT_IEEE_FLOAT
  le16(out, 2);  // stereo
  le32(out, kSampleRate);
  le32(out, kSampleRate * 2 * sizeof(float));
  le16(out, 2 * sizeof(float));
  le16(out, 8 * sizeof(float));
  le16(out, 0);  // cbSize

  // fact chunk (required for non-PCM formats)
  out.write("fact", 4);
  le32(out, 4);
  le32(out, static_cast<std::uint32_t>(frames));

  // data chunk
  out.write("data", 4);
  le32(out, data_size);

  std::vector<float> interleaved(8192);
  for (std::size_t first = 0; first < audio.frames();) {
    const std::size_t count = std::min<std::size_t>(4096, audio.frames() - first);
    for (std::size_t i = 0; i < count; ++i) {
      interleaved[2 * i] = audio.channels[0][first + i];
      interleaved[2 * i + 1] = audio.channels[1][first + i];
    }
    out.write(reinterpret_cast<const char*>(interleaved.data()),
              static_cast<std::streamsize>(count * 2 * sizeof(float)));
    first += count;
  }
  if (!out) throw Error("synth_failed", "Failed while writing stem WAV");
}

}  // namespace cgmusic
