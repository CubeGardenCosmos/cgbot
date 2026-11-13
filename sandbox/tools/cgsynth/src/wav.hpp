#pragma once

#include <filesystem>

#include "common.hpp"

namespace cgmusic {

// IEEE 754 float、44.1 kHz、立体声；不做归一化、裁剪、抖动或格式转换。
void write_float_wav(const Audio& audio, const std::filesystem::path& path);

}  // namespace cgmusic
