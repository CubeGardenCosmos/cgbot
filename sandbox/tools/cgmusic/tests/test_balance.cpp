#include <cstdint>

#include "balance.hpp"
#include "sends.hpp"
#include "test_harness.hpp"

namespace cgmusic {
namespace {

MidiEvent make_event(std::uint8_t status, std::uint8_t data1, std::uint8_t data2) {
  MidiEvent event;
  event.status = status;
  event.data1 = data1;
  event.data2 = data2;
  return event;
}

}  // namespace

CGMUSIC_TEST(classify_programs_to_stems) {
  CGMUSIC_CHECK(classify_note(0, 21, 60) == Stem::Accordion);
  CGMUSIC_CHECK(classify_note(0, 22, 60) == Stem::Melodic);  // 口琴不归入手风琴
  CGMUSIC_CHECK(classify_note(0, 23, 60) == Stem::Accordion);
  CGMUSIC_CHECK(classify_note(0, 56, 60) == Stem::Brass);
  CGMUSIC_CHECK(classify_note(0, 63, 60) == Stem::Brass);
  CGMUSIC_CHECK(classify_note(0, 64, 60) == Stem::Woodwind);
  CGMUSIC_CHECK(classify_note(0, 79, 60) == Stem::Woodwind);
  CGMUSIC_CHECK(classify_note(0, 80, 60) == Stem::Melodic);
}

CGMUSIC_TEST(classify_drum_notes_by_note_number) {
  CGMUSIC_CHECK(classify_note(9, 0, 35) == Stem::Kick);
  CGMUSIC_CHECK(classify_note(9, 0, 36) == Stem::Kick);
  CGMUSIC_CHECK(classify_note(9, 0, 38) == Stem::Snare);
  CGMUSIC_CHECK(classify_note(9, 0, 40) == Stem::Snare);
  CGMUSIC_CHECK(classify_note(9, 0, 42) == Stem::OtherDrum);
  // 鼓组通道只看音符号,与程序号无关。
  CGMUSIC_CHECK(classify_note(9, 64, 36) == Stem::Kick);
}

CGMUSIC_TEST(stem_gains_apply_base_and_offsets) {
  // 非鼓组:-6 dB 基础电平加偏移。
  CGMUSIC_CHECK(stem_gain_db(Stem::Melodic, true) == -6.0);
  CGMUSIC_CHECK(stem_gain_db(Stem::Brass, true) == 0.0);       // -6 + 6
  CGMUSIC_CHECK(stem_gain_db(Stem::Woodwind, true) == -12.0);  // -6 + -6
  CGMUSIC_CHECK(stem_gain_db(Stem::Accordion, true) == -12.0);
  // 鼓组:绝对增益,不叠加基础电平。
  CGMUSIC_CHECK(stem_gain_db(Stem::Kick, true) == 6.0);
  CGMUSIC_CHECK(stem_gain_db(Stem::Snare, true) == 0.0);
  CGMUSIC_CHECK(stem_gain_db(Stem::OtherDrum, true) == -6.0);
  // 关闭平衡:全部为 0 dB。
  CGMUSIC_CHECK(stem_gain_db(Stem::Brass, false) == 0.0);
  CGMUSIC_CHECK(stem_gain_db(Stem::Kick, false) == 0.0);
}

CGMUSIC_TEST(router_routes_notes_and_releases_to_owner) {
  StemRouter router(true);
  CGMUSIC_CHECK(router.route(make_event(0xC0, 64, 0)) == kAllStems);
  CGMUSIC_CHECK(router.route(make_event(0x90, 60, 100)) == stem_bit(Stem::Woodwind));
  CGMUSIC_CHECK(router.route(make_event(0xB0, 7, 100)) == kAllStems);
  CGMUSIC_CHECK(router.route(make_event(0x80, 60, 0)) == stem_bit(Stem::Woodwind));
  // 已释放的音符没有归属。
  CGMUSIC_CHECK(router.route(make_event(0x80, 60, 0)) == 0);
}

CGMUSIC_TEST(router_keeps_note_owner_across_program_change) {
  StemRouter router(true);
  router.route(make_event(0xC0, 56, 0));  // 铜管
  CGMUSIC_CHECK(router.route(make_event(0x90, 60, 100)) == stem_bit(Stem::Brass));
  router.route(make_event(0xC0, 0, 0));  // 钢琴
  CGMUSIC_CHECK(router.route(make_event(0x80, 60, 0)) == stem_bit(Stem::Brass));
}

CGMUSIC_TEST(router_without_balance_uses_melodic_only) {
  StemRouter router(false);
  router.route(make_event(0xC0, 64, 0));
  CGMUSIC_CHECK(router.route(make_event(0x90, 60, 100)) == stem_bit(Stem::Melodic));
  CGMUSIC_CHECK(router.route(make_event(0x80, 60, 0)) == stem_bit(Stem::Melodic));
}

CGMUSIC_TEST(default_sends_cover_every_program) {
  for (int program = 0; program < 128; ++program) {
    CGMUSIC_CHECK(default_sends(0, program).reverb > 0);
  }
  CGMUSIC_CHECK(default_sends(0, 40).reverb == 55);
  CGMUSIC_CHECK(default_sends(0, 48).reverb == 60);
  CGMUSIC_CHECK(default_sends(0, 88).chorus == 15);
  CGMUSIC_CHECK(default_sends(0, 127).reverb == 40);
  CGMUSIC_CHECK(default_sends(9, 0).reverb == 15);
}

}  // namespace cgmusic
