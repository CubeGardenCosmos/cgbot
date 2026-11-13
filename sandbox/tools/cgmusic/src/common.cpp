#include "common.hpp"

#include <array>
#include <cstdio>

namespace cgmusic {

std::vector<std::uint8_t> read_file(const std::filesystem::path& path,
                                    std::size_t maximum) {
  std::error_code ec;
  const auto size = std::filesystem::file_size(path, ec);
  if (ec) throw Error("bad_input", "Cannot open input file");
  if (size > maximum) throw Error("bad_input", "Input file exceeds size limit");
  FILE* file = std::fopen(path.c_str(), "rb");
  if (!file) throw Error("bad_input", "Cannot open input file");
  std::vector<std::uint8_t> data(static_cast<std::size_t>(size));
  std::size_t offset = 0;
  while (offset < data.size()) {
    const auto count = std::fread(data.data() + offset, 1, data.size() - offset, file);
    if (count == 0) {
      std::fclose(file);
      throw Error("bad_input", "Could not read complete input file");
    }
    offset += count;
  }
  std::array<unsigned char, 1> extra{};
  const bool too_large = std::fread(extra.data(), 1, 1, file) != 0;
  const bool failed = std::ferror(file) != 0;
  std::fclose(file);
  if (too_large) throw Error("bad_input", "Input file exceeds size limit");
  if (failed) throw Error("bad_input", "Could not read input file");
  return data;
}

void write_file(const std::filesystem::path& path,
                const std::vector<std::uint8_t>& data) {
  FILE* file = std::fopen(path.c_str(), "wb");
  if (!file) throw Error("synth_failed", "Cannot create output file");
  const bool ok = data.empty() || std::fwrite(data.data(), 1, data.size(), file) == data.size();
  const bool closed = std::fclose(file) == 0;
  if (!ok || !closed) {
    std::error_code ec;
    std::filesystem::remove(path, ec);
    throw Error("synth_failed", "Could not write output file");
  }
}

std::string json_string(std::string_view value) {
  static constexpr char hex[] = "0123456789abcdef";
  std::string result;
  result.reserve(value.size() + 2);
  result.push_back('"');
  for (const unsigned char ch : value) {
    switch (ch) {
      case '"': result += "\\\""; break;
      case '\\': result += "\\\\"; break;
      case '\b': result += "\\b"; break;
      case '\f': result += "\\f"; break;
      case '\n': result += "\\n"; break;
      case '\r': result += "\\r"; break;
      case '\t': result += "\\t"; break;
      default:
        if (ch < 0x20) {
          result += "\\u00";
          result.push_back(hex[ch >> 4]);
          result.push_back(hex[ch & 15]);
        } else {
          result.push_back(static_cast<char>(ch));
        }
    }
  }
  result.push_back('"');
  return result;
}
std::string format_error_json(const std::string& code, const std::string& message) {
  return "{\"error\":" + json_string(code) + ",\"message\":" + json_string(message) + "}";
}

}  // namespace cgmusic
