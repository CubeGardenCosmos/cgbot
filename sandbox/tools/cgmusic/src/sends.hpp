#pragma once

#include <array>
#include <cstdint>
#include <optional>
#include <string_view>

#include "midi.hpp"

namespace cgmusic {

constexpr int kReverbCc = 91;  // CC91 混响发送
constexpr int kChorusCc = 93;  // CC93 合唱发送

// 默认发送量:MIDI CC91(混响)与 CC93(合唱),取值 0-127。
struct SendDefaults {
  std::uint8_t reverb;
  std::uint8_t chorus;
};

// 按通道与 GM 程序号给出默认发送量。第 10 通道(索引 9)为鼓组,只看鼓组行,与程序号无关。
SendDefaults default_sends(int channel, int program) noexcept;

// 发送量来源,优先级从高到低:--reverb 显式覆盖、文件中的显式 CC、默认表。
enum class SendSource { Table, File, Cli };

// 来源名称(用于 JSON 报告):table / file / cli。
std::string_view send_source_name(SendSource source) noexcept;

// --reverb 显式覆盖,按通道索引 0-15;未设置的通道为 nullopt。
using CliReverb = std::array<std::optional<int>, 16>;

// 单通道的发送状态。
struct ChannelSends {
  bool used = false;               // 本通道是否出现过音符
  int program = 0;                 // 最后一次程序号
  std::optional<int> file_reverb;  // 文件中的显式 CC91;一旦设置即保持到文件末尾
  std::optional<int> file_chorus;  // 文件中的显式 CC93;一旦设置即保持到文件末尾
  int reverb = 0;                  // 最终混响发送
  int chorus = 0;                  // 最终合唱发送
  SendSource reverb_source = SendSource::Table;
  SendSource chorus_source = SendSource::Table;
};

// 通道的初始状态:程序 0,无文件显式值。
ChannelSends initial_sends(const CliReverb& cli_reverb, int channel) noexcept;

// 处理一个事件:仅 PC、CC91、CC93 会改变发送状态,其它事件忽略。
// 文件中 tick 0 处值为 0 的 CC91/CC93 视为未设置(多数编曲软件以 0 作为初始化值)。
void update_sends(const CliReverb& cli_reverb, int channel, const MidiEvent& event,
                  ChannelSends& state) noexcept;

}  // namespace cgmusic
