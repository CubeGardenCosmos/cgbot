#include <array>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <string>
#include <vector>

#include "test_harness.hpp"
#include "wav.hpp"

namespace {
std::uint16_t u16(const std::vector<char>& bytes, std::size_t at) {
  return static_cast<std::uint16_t>(static_cast<unsigned char>(bytes[at])) |
         (static_cast<std::uint16_t>(static_cast<unsigned char>(bytes[at + 1])) << 8);
}
std::uint32_t u32(const std::vector<char>& bytes, std::size_t at) {
  return static_cast<std::uint32_t>(static_cast<unsigned char>(bytes[at])) |
         (static_cast<std::uint32_t>(static_cast<unsigned char>(bytes[at + 1])) << 8) |
         (static_cast<std::uint32_t>(static_cast<unsigned char>(bytes[at + 2])) << 16) |
         (static_cast<std::uint32_t>(static_cast<unsigned char>(bytes[at + 3])) << 24);
}
float f32(const std::vector<char>& bytes, std::size_t at) {
  const std::uint32_t bits = u32(bytes, at);
  float value = 0;
  std::memcpy(&value, &bits, sizeof(value));
  return value;
}
}  // namespace

CGMUSIC_TEST(stem_wav_is_unmodified_stereo_float_with_exact_length) {
  cgmusic::Audio audio;
  audio.channels.assign(2, std::vector<float>(513, 0.0F));
  audio.channels[0][0] = 0.125F;
  audio.channels[0][512] = 2.0F;  // 不限制、不归一化。
  audio.channels[1][0] = -0.25F;
  audio.channels[1][512] = -3.0F;
  const auto path = std::filesystem::temp_directory_path() / "cgsynth_stem_test.wav";
  cgmusic::write_float_wav(audio, path);
  std::ifstream in(path, std::ios::binary);
  const std::vector<char> bytes((std::istreambuf_iterator<char>(in)), {});
  std::filesystem::remove(path);

  CGMUSIC_CHECK(bytes.size() == 12 + 26 + 12 + 8 + 513 * 2 * sizeof(float));
  CGMUSIC_CHECK(std::string(bytes.data(), 4) == "RIFF");
  CGMUSIC_CHECK(std::string(bytes.data() + 8, 4) == "WAVE");
  CGMUSIC_CHECK(u16(bytes, 20) == 3);  // WAVE_FORMAT_IEEE_FLOAT
  CGMUSIC_CHECK(u16(bytes, 22) == 2);  // stereo
  CGMUSIC_CHECK(u32(bytes, 24) == 44100);
  CGMUSIC_CHECK(u16(bytes, 34) == 32);  // 32-bit float
  std::size_t data = 12;
  while (data + 8 <= bytes.size() && std::string(bytes.data() + data, 4) != "data") {
    data += 8 + u32(bytes, data + 4) + (u32(bytes, data + 4) & 1U);
  }
  CGMUSIC_CHECK(data + 8 <= bytes.size());
  CGMUSIC_CHECK(u32(bytes, data + 4) == 513 * 2 * sizeof(float));
  const std::size_t samples = data + 8;
  CGMUSIC_CHECK(f32(bytes, samples) == 0.125F && f32(bytes, samples + 4) == -0.25F);
  CGMUSIC_CHECK(f32(bytes, samples + 512 * 8) == 2.0F);
  CGMUSIC_CHECK(f32(bytes, samples + 512 * 8 + 4) == -3.0F);
}
