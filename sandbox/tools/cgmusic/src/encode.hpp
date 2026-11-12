#pragma once

#include <filesystem>
#include <string_view>

#include "common.hpp"

namespace cgmusic {

enum class AudioFormat { Mp3, Wav, Flac };

AudioFormat parse_format(std::string_view name);
std::string_view format_name(AudioFormat format);

// wav/flac 使用 libsndfile(24 位 PCM);mp3 使用 libmp3lame(CBR 192 kbps,立体声)。
void encode_audio(const Audio& audio, AudioFormat format, const std::filesystem::path& path);

// 通过 libsndfile 读取 WAV/FLAC/OGG/MP3(取决于 libsndfile 构建)为 float 平面音频。
Audio read_audio(const std::filesystem::path& path);

}  // namespace cgmusic
