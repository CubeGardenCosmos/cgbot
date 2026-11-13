#pragma once

#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace cgmusic {

inline constexpr std::size_t kMaxInputBytes = 32U * 1024U * 1024U;
inline constexpr std::size_t kMaxMidiBytes = 8U * 1024U * 1024U;
inline constexpr double kMaxMidiSeconds = 15.0 * 60.0;
inline constexpr unsigned kSampleRate = 44100;

// 错误码:bad_input, too_long, no_soundfont, synth_failed, bad_musicxml, unsupported, muse_unavailable, muse_version。
class Error final : public std::runtime_error {
 public:
  Error(std::string code, const std::string& message)
      : std::runtime_error(message), code_(std::move(code)) {}

  const std::string& code() const noexcept { return code_; }

 private:
  std::string code_;
};

// 平面 float 音频;各声道长度相同。
struct Audio {
  unsigned rate = kSampleRate;
  std::vector<std::vector<float>> channels;

  std::size_t frames() const noexcept {
    return channels.empty() ? 0 : channels.front().size();
  }
};

std::vector<std::uint8_t> read_file(const std::filesystem::path& path, std::size_t maximum);
void write_file(const std::filesystem::path& path, const std::vector<std::uint8_t>& data);
std::string json_string(std::string_view value);
std::string format_error_json(const std::string& code, const std::string& message);

}  // namespace cgmusic
