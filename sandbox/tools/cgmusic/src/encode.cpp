#include "encode.hpp"

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <system_error>
#include <vector>

#include <lame/lame.h>
#include <sndfile.h>

namespace fs = std::filesystem;

namespace cgmusic {
namespace {

constexpr std::size_t kChunkFrames = 4096;
constexpr int kMp3Bitrate = 192;

struct LameDeleter {
  void operator()(lame_global_flags* flags) const { lame_close(flags); }
};
using LamePtr = std::unique_ptr<lame_global_flags, LameDeleter>;

void write_sndfile(const Audio& audio, int format, const fs::path& path) {
  SF_INFO info{};
  info.samplerate = static_cast<int>(audio.rate);
  info.channels = static_cast<int>(audio.channels.size());
  info.format = format;
  if (sf_format_check(&info) == 0) throw Error("synth_failed", "Unsupported audio format");

  SNDFILE* file = sf_open(path.string().c_str(), SFM_WRITE, &info);
  if (!file) throw Error("synth_failed", "Cannot create audio file");

  const std::size_t channels = audio.channels.size();
  const std::size_t frames = audio.frames();
  std::vector<float> interleaved(kChunkFrames * channels);
  bool ok = true;
  for (std::size_t start = 0; ok && start < frames; start += kChunkFrames) {
    const std::size_t count = std::min(kChunkFrames, frames - start);
    for (std::size_t f = 0; f < count; ++f) {
      for (std::size_t c = 0; c < channels; ++c) {
        interleaved[f * channels + c] = audio.channels[c][start + f];
      }
    }
    if (sf_writef_float(file, interleaved.data(), static_cast<sf_count_t>(count)) !=
        static_cast<sf_count_t>(count)) {
      ok = false;
    }
  }
  const int closed = sf_close(file);
  if (!ok || closed != 0) {
    std::error_code ec;
    fs::remove(path, ec);
    throw Error("synth_failed", "Could not write audio file");
  }
}

void write_mp3(const Audio& audio, const fs::path& path) {
  if (audio.channels.size() != 2) throw Error("unsupported", "MP3 output requires stereo audio");

  LamePtr flags(lame_init());
  if (!flags) throw Error("synth_failed", "LAME initialisation failed");
  lame_set_in_samplerate(flags.get(), static_cast<int>(audio.rate));
  lame_set_out_samplerate(flags.get(), static_cast<int>(audio.rate));
  lame_set_num_channels(flags.get(), 2);
  lame_set_mode(flags.get(), JOINT_STEREO);
  lame_set_quality(flags.get(), 2);
  lame_set_brate(flags.get(), kMp3Bitrate);
  if (lame_init_params(flags.get()) < 0) throw Error("synth_failed", "LAME parameters rejected");

  const std::size_t frames = audio.frames();
  std::vector<short> left(kChunkFrames);
  std::vector<short> right(kChunkFrames);
  std::vector<std::uint8_t> buffer(kChunkFrames * 5 / 4 + 7200);
  std::vector<std::uint8_t> output;

  const auto to_pcm = [](float value) {
    const float scaled = std::clamp(value, -1.0F, 1.0F) * 32767.0F;
    return static_cast<short>(std::lround(scaled));
  };

  for (std::size_t start = 0; start < frames; start += kChunkFrames) {
    const std::size_t count = std::min(kChunkFrames, frames - start);
    for (std::size_t f = 0; f < count; ++f) {
      left[f] = to_pcm(audio.channels[0][start + f]);
      right[f] = to_pcm(audio.channels[1][start + f]);
    }
    const int written = lame_encode_buffer(flags.get(), left.data(), right.data(),
                                           static_cast<int>(count), buffer.data(),
                                           static_cast<int>(buffer.size()));
    if (written < 0) throw Error("synth_failed", "MP3 encoding failed");
    output.insert(output.end(), buffer.begin(), buffer.begin() + written);
  }
  const int flushed =
      lame_encode_flush(flags.get(), buffer.data(), static_cast<int>(buffer.size()));
  if (flushed < 0) throw Error("synth_failed", "MP3 encoding failed");
  output.insert(output.end(), buffer.begin(), buffer.begin() + flushed);
  write_file(path, output);
}

}  // namespace

AudioFormat parse_format(std::string_view name) {
  if (name == "mp3") return AudioFormat::Mp3;
  if (name == "wav") return AudioFormat::Wav;
  if (name == "flac") return AudioFormat::Flac;
  throw Error("bad_input", "Unsupported output format: " + std::string(name));
}

std::string_view format_name(AudioFormat format) {
  switch (format) {
    case AudioFormat::Mp3: return "mp3";
    case AudioFormat::Wav: return "wav";
    case AudioFormat::Flac: return "flac";
  }
  return "mp3";
}

void encode_audio(const Audio& audio, AudioFormat format, const fs::path& path) {
  switch (format) {
    case AudioFormat::Mp3:
      write_mp3(audio, path);
      break;
    case AudioFormat::Wav:
      write_sndfile(audio, SF_FORMAT_WAV | SF_FORMAT_PCM_24, path);
      break;
    case AudioFormat::Flac:
      write_sndfile(audio, SF_FORMAT_FLAC | SF_FORMAT_PCM_24, path);
      break;
  }
}

Audio read_audio(const fs::path& path) {
  SF_INFO info{};
  SNDFILE* file = sf_open(path.string().c_str(), SFM_READ, &info);
  if (!file) throw Error("unsupported", "Unsupported or unreadable audio file");
  if (info.channels < 1 || info.channels > 5 || info.samplerate < 16000) {
    sf_close(file);
    throw Error("unsupported", "Unsupported audio channel layout or sample rate");
  }

  const auto channels = static_cast<std::size_t>(info.channels);
  Audio audio;
  audio.rate = static_cast<unsigned>(info.samplerate);
  audio.channels.assign(channels, {});
  std::vector<float> interleaved(kChunkFrames * channels);
  for (;;) {
    const sf_count_t got = sf_readf_float(file, interleaved.data(), kChunkFrames);
    if (got <= 0) break;
    for (sf_count_t f = 0; f < got; ++f) {
      for (std::size_t c = 0; c < channels; ++c) {
        audio.channels[c].push_back(interleaved[static_cast<std::size_t>(f) * channels + c]);
      }
    }
  }
  sf_close(file);
  return audio;
}

}  // namespace cgmusic
