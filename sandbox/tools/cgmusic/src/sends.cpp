#include "sends.hpp"

#include <cstddef>

namespace cgmusic {
namespace {

constexpr int kDrumChannel = 9;  // MIDI 通道 10

struct ProgramSendRow {
  int first;
  int last;
  SendDefaults sends;
};

// cgmusic 自行调校的默认发送量(非 MuseScore 数值:MuseScore 不向 FluidSynth 发送混响,其数值不可取得)。
// 按 GM 乐器族给出 CC91(混响)与 CC93(合唱);覆盖 0-127 且连续。调用方可用 --reverb 覆盖混响。
constexpr ProgramSendRow kProgramSends[] = {
    {0, 7, {30, 0}},      // 钢琴
    {8, 15, {25, 0}},     // 色彩打击乐
    {16, 23, {35, 0}},    // 风琴
    {24, 31, {25, 0}},    // 吉他
    {32, 39, {10, 0}},    // 贝斯
    {40, 47, {55, 0}},    // 弦乐
    {48, 51, {60, 0}},    // 合奏弦乐 / 合成弦乐
    {52, 54, {65, 0}},    // 人声
    {55, 55, {60, 0}},    // 管弦乐打击
    {56, 63, {40, 0}},    // 铜管
    {64, 71, {35, 0}},    // 簧管
    {72, 79, {35, 0}},    // 笛类
    {80, 87, {30, 0}},    // 合成主音
    {88, 95, {65, 15}},   // 合成铺底
    {96, 103, {60, 0}},   // 合成音效
    {104, 111, {35, 0}},  // 民族乐器
    {112, 119, {20, 0}},  // 打击乐器
    {120, 127, {40, 0}},  // 音效
};

// 鼓组默认混响(cgmusic 自行调校)。
constexpr SendDefaults kDrumSends{15, 0};

// 按优先级解析发送量:--reverb 显式覆盖 > 文件中的显式值 > 默认表。
void resolve(const CliReverb& cli_reverb, int channel, ChannelSends& state) noexcept {
  const SendDefaults defaults = default_sends(channel, state.program);
  if (cli_reverb[channel]) {
    state.reverb = *cli_reverb[channel];
    state.reverb_source = SendSource::Cli;
  } else if (state.file_reverb) {
    state.reverb = *state.file_reverb;
    state.reverb_source = SendSource::File;
  } else {
    state.reverb = defaults.reverb;
    state.reverb_source = SendSource::Table;
  }
  if (state.file_chorus) {
    state.chorus = *state.file_chorus;
    state.chorus_source = SendSource::File;
  } else {
    state.chorus = defaults.chorus;
    state.chorus_source = SendSource::Table;
  }
}

// 文件中的 CC 值;tick 0 处的 0 视为未设置。
std::optional<int> file_value(const MidiEvent& event) noexcept {
  if (event.data2 == 0 && event.seconds <= 0.0) return std::nullopt;
  return event.data2;
}

}  // namespace

SendDefaults default_sends(int channel, int program) noexcept {
  if (channel == kDrumChannel) return kDrumSends;
  for (const ProgramSendRow& row : kProgramSends) {
    if (program >= row.first && program <= row.last) return row.sends;
  }
  // 表覆盖 0-127 的全部程序号;超出范围时不发送。
  return SendDefaults{0, 0};
}

std::string_view send_source_name(SendSource source) noexcept {
  switch (source) {
    case SendSource::Cli:
      return "cli";
    case SendSource::File:
      return "file";
    case SendSource::Table:
      break;
  }
  return "table";
}

ChannelSends initial_sends(const CliReverb& cli_reverb, int channel) noexcept {
  ChannelSends state;
  resolve(cli_reverb, channel, state);
  return state;
}

void update_sends(const CliReverb& cli_reverb, int channel, const MidiEvent& event,
                  ChannelSends& state) noexcept {
  switch (event.status & 0xF0) {
    case 0xB0:
      if (event.data1 == kReverbCc) {
        if (const auto value = file_value(event)) state.file_reverb = value;
      } else if (event.data1 == kChorusCc) {
        if (const auto value = file_value(event)) state.file_chorus = value;
      } else {
        return;
      }
      break;
    case 0xC0:
      state.program = event.data1;
      break;
    default:
      return;
  }
  resolve(cli_reverb, channel, state);
}

}  // namespace cgmusic
