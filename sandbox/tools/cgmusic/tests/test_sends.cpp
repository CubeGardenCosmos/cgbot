#include <cstdint>

#include "sends.hpp"
#include "test_harness.hpp"

namespace cgmusic {
namespace {

constexpr std::uint8_t kNoteOn = 0x90;
constexpr std::uint8_t kCc = 0xB0;
constexpr std::uint8_t kPc = 0xC0;

MidiEvent make_event(std::uint8_t status, std::uint8_t data1, std::uint8_t data2,
                     double seconds = 0.0) {
  MidiEvent event;
  event.seconds = seconds;
  event.status = status;
  event.data1 = data1;
  event.data2 = data2;
  return event;
}

}  // namespace

CGMUSIC_TEST(sends_table_default_follows_program_change) {
  const CliReverb cli{};
  ChannelSends state = initial_sends(cli, 0);
  CGMUSIC_CHECK(state.reverb == default_sends(0, 0).reverb);
  CGMUSIC_CHECK(state.reverb_source == SendSource::Table);
  update_sends(cli, 0, make_event(kPc, 40, 0), state);
  CGMUSIC_CHECK(state.program == 40);
  CGMUSIC_CHECK(state.reverb == 55);
  CGMUSIC_CHECK(state.reverb_source == SendSource::Table);
  CGMUSIC_CHECK(state.chorus_source == SendSource::Table);
}

CGMUSIC_TEST(sends_file_value_at_tick_zero_beats_table_regardless_of_order) {
  const CliReverb cli{};
  ChannelSends state = initial_sends(cli, 0);
  update_sends(cli, 0, make_event(kCc, 91, 40), state);
  CGMUSIC_CHECK(state.reverb == 40);
  CGMUSIC_CHECK(state.reverb_source == SendSource::File);
  // 程序号之后到达也不覆盖文件值。
  update_sends(cli, 0, make_event(kPc, 40, 0), state);
  CGMUSIC_CHECK(state.reverb == 40);
  CGMUSIC_CHECK(state.reverb_source == SendSource::File);
}

CGMUSIC_TEST(sends_program_change_before_file_value_is_overridden) {
  const CliReverb cli{};
  ChannelSends state = initial_sends(cli, 0);
  update_sends(cli, 0, make_event(kPc, 48, 0), state);
  CGMUSIC_CHECK(state.reverb == 60);
  update_sends(cli, 0, make_event(kCc, 91, 25), state);
  CGMUSIC_CHECK(state.reverb == 25);
  CGMUSIC_CHECK(state.reverb_source == SendSource::File);
}

CGMUSIC_TEST(sends_zero_at_tick_zero_is_unset) {
  const CliReverb cli{};
  ChannelSends state = initial_sends(cli, 0);
  update_sends(cli, 0, make_event(kCc, 91, 0, 0.0), state);
  CGMUSIC_CHECK(state.reverb == default_sends(0, 0).reverb);
  CGMUSIC_CHECK(state.reverb_source == SendSource::Table);
  update_sends(cli, 0, make_event(kCc, 93, 0, 0.0), state);
  CGMUSIC_CHECK(state.chorus_source == SendSource::Table);
}

CGMUSIC_TEST(sends_zero_after_tick_zero_is_explicit) {
  const CliReverb cli{};
  ChannelSends state = initial_sends(cli, 0);
  update_sends(cli, 0, make_event(kCc, 91, 0, 1.5), state);
  CGMUSIC_CHECK(state.reverb == 0);
  CGMUSIC_CHECK(state.reverb_source == SendSource::File);
  // 显式零值之后的程序号变化同样不覆盖。
  update_sends(cli, 0, make_event(kPc, 88, 0, 2.0), state);
  CGMUSIC_CHECK(state.reverb == 0);
  CGMUSIC_CHECK(state.reverb_source == SendSource::File);
}

CGMUSIC_TEST(sends_cli_override_beats_file_and_table) {
  CliReverb cli{};
  cli[0] = 10;
  ChannelSends state = initial_sends(cli, 0);
  CGMUSIC_CHECK(state.reverb == 10);
  CGMUSIC_CHECK(state.reverb_source == SendSource::Cli);
  update_sends(cli, 0, make_event(kCc, 91, 40), state);
  CGMUSIC_CHECK(state.reverb == 10);
  CGMUSIC_CHECK(state.reverb_source == SendSource::Cli);
  update_sends(cli, 0, make_event(kPc, 88, 0), state);
  CGMUSIC_CHECK(state.reverb == 10);
  CGMUSIC_CHECK(state.reverb_source == SendSource::Cli);
}

CGMUSIC_TEST(sends_cli_override_is_per_channel) {
  CliReverb cli{};
  cli[0] = 10;
  const ChannelSends other = initial_sends(cli, 1);
  CGMUSIC_CHECK(other.reverb == default_sends(1, 0).reverb);
  CGMUSIC_CHECK(other.reverb_source == SendSource::Table);
}

CGMUSIC_TEST(sends_chorus_file_value_is_kept) {
  const CliReverb cli{};
  ChannelSends state = initial_sends(cli, 0);
  update_sends(cli, 0, make_event(kCc, 93, 20), state);
  update_sends(cli, 0, make_event(kPc, 88, 0), state);
  CGMUSIC_CHECK(state.chorus == 20);
  CGMUSIC_CHECK(state.chorus_source == SendSource::File);
  CGMUSIC_CHECK(state.reverb_source == SendSource::Table);
}

CGMUSIC_TEST(sends_chorus_table_applies_without_file_value) {
  const CliReverb cli{};
  ChannelSends state = initial_sends(cli, 0);
  update_sends(cli, 0, make_event(kPc, 88, 0), state);
  CGMUSIC_CHECK(state.chorus == default_sends(0, 88).chorus);
  CGMUSIC_CHECK(state.chorus == 15);
  CGMUSIC_CHECK(state.chorus_source == SendSource::Table);
}

CGMUSIC_TEST(sends_drum_channel_ignores_program) {
  const CliReverb cli{};
  ChannelSends state = initial_sends(cli, 9);
  CGMUSIC_CHECK(state.reverb == default_sends(9, 0).reverb);
  update_sends(cli, 9, make_event(0xC9, 40, 0), state);
  CGMUSIC_CHECK(state.reverb == default_sends(9, 40).reverb);
  CGMUSIC_CHECK(state.reverb == 15);
}

CGMUSIC_TEST(sends_non_send_events_are_ignored) {
  const CliReverb cli{};
  ChannelSends state = initial_sends(cli, 0);
  const ChannelSends before = state;
  update_sends(cli, 0, make_event(kNoteOn, 60, 100), state);
  update_sends(cli, 0, make_event(kCc, 7, 100), state);
  CGMUSIC_CHECK(state.program == before.program);
  CGMUSIC_CHECK(state.reverb == before.reverb);
  CGMUSIC_CHECK(state.chorus == before.chorus);
  CGMUSIC_CHECK(!state.file_reverb.has_value());
}

CGMUSIC_TEST(send_source_names_match_report_vocabulary) {
  CGMUSIC_CHECK(send_source_name(SendSource::Cli) == "cli");
  CGMUSIC_CHECK(send_source_name(SendSource::File) == "file");
  CGMUSIC_CHECK(send_source_name(SendSource::Table) == "table");
}

}  // namespace cgmusic
