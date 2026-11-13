#include <algorithm>
#include <cmath>
#include <cstddef>
#include <functional>
#include <string>
#include <vector>

#include "common.hpp"
#include "muse/mapping.hpp"
#include "muse/render.hpp"
#include "musicxml.hpp"
#include "test_harness.hpp"


namespace {

using cgmusic::Audio;
using cgmusic::Catalog;
using cgmusic::MidiEvent;
using cgmusic::MidiSequence;
using cgmusic::MidiTempo;
using cgmusic::RenderOptions;
using cgmusic::muse::ChannelPlan;

Catalog catalog() {
  return {{91, "Violins 1", "Vlns.", "Muse Strings", "strings"},
          {110, "Trumpet", "Tpt.", "Muse Brass", "brass"},
          {118, "Flute 1", "Fl.", "Muse Woodwinds", "woodwinds"},
          {188, "GM Kit", "Perc.", "Muse Percussion", "percussion"}};
}

MidiEvent note(double time, int channel, int pitch, int velocity) {
  return {time, static_cast<std::uint8_t>(0x90 | channel), static_cast<std::uint8_t>(pitch),
          static_cast<std::uint8_t>(velocity)};
}

MidiEvent off(double time, int channel, int pitch) {
  return {time, static_cast<std::uint8_t>(0x80 | channel), static_cast<std::uint8_t>(pitch), 0};
}

std::string error_code_of(const std::function<void()>& action) {
  try {
    action();
  } catch (const cgmusic::Error& error) {
    return error.code();
  }
  return "none";
}

bool near(double a, double b, double epsilon = 1e-6) { return std::fabs(a - b) <= epsilon; }

}  // namespace

CGMUSIC_TEST(muse_engine_parser_never_maps_muse_to_fluidsynth) {
  CGMUSIC_CHECK(cgmusic::parse_render_engine("fluidsynth") == cgmusic::RenderEngine::FluidSynth);
  CGMUSIC_CHECK(cgmusic::parse_render_engine("muse") == cgmusic::RenderEngine::Muse);
  CGMUSIC_CHECK(error_code_of([] { cgmusic::parse_render_engine("Muse"); }) == "bad_input");
  CGMUSIC_CHECK(error_code_of([] { cgmusic::parse_render_engine(""); }) == "bad_input");
}

CGMUSIC_TEST(muse_plan_pairs_notes_and_applies_tempo_and_velocity) {
  MidiSequence sequence;
  sequence.duration_s = 2.0;
  sequence.tempo_map = {{0, 0.0, 500'000}, {480, 1.0, 250'000}};
  sequence.events = {
      {0.0, 0xC0, 73, 0},
      {0.0, 0xC1, 56, 0},
      note(0.0, 0, 72, 70),
      note(0.25, 1, 64, 90),
      off(0.5, 0, 72),
      off(1.0, 1, 64),
      note(1.5, 0, 74, 100),
      off(2.0, 0, 74),
  };
  const auto plans = cgmusic::muse::plan_channels(sequence, RenderOptions{}, catalog());
  CGMUSIC_CHECK(plans.size() == 2);
  CGMUSIC_CHECK(plans[0].instrument_id == 118);
  CGMUSIC_CHECK(plans[0].program == 73);
  CGMUSIC_CHECK(plans[0].notes.size() == 2);
  CGMUSIC_CHECK(plans[0].notes[0]._location_us == 0);
  CGMUSIC_CHECK(plans[0].notes[0]._duration_us == 500'000);
  CGMUSIC_CHECK(near(plans[0].notes[0]._tempo, 120.0));
  CGMUSIC_CHECK(plans[0].notes[1]._location_us == 1'500'000);
  CGMUSIC_CHECK(near(plans[0].notes[1]._tempo, 240.0));
  CGMUSIC_CHECK(plans[0].dynamics.size() == 1);
  CGMUSIC_CHECK(near(plans[0].dynamics[0]._value, 100.0 / 127.0));
  CGMUSIC_CHECK(plans[1].instrument_id == 110);
  CGMUSIC_CHECK(plans[1].notes.size() == 1);
  CGMUSIC_CHECK(plans[1].notes[0]._duration_us == 750'000);
}

CGMUSIC_TEST(muse_plan_uses_last_sounding_program_and_explicit_send) {
  MidiSequence sequence;
  sequence.duration_s = 1.0;
  sequence.events = {{0.0, 0xC0, 73, 0}, {0.0, 0xC0, 56, 0}, note(0.0, 0, 60, 100),
                     off(1.0, 0, 60)};
  RenderOptions options;
  options.reverb.channel_send[0] = 17;
  const auto plans = cgmusic::muse::plan_channels(sequence, options, catalog());
  CGMUSIC_CHECK(plans.size() == 1);
  CGMUSIC_CHECK(plans[0].program == 56);
  CGMUSIC_CHECK(plans[0].instrument_id == 110);
  CGMUSIC_CHECK(plans[0].sends.reverb == 17);
  CGMUSIC_CHECK(plans[0].sends.reverb_source == cgmusic::SendSource::Cli);
}

CGMUSIC_TEST(muse_plan_closes_unfinished_and_overlapping_notes) {
  MidiSequence sequence;
  sequence.duration_s = 2.0;
  sequence.events = {{0.0, 0xC0, 73, 0}, note(0.0, 0, 60, 80), note(0.5, 0, 60, 90),
                     off(1.0, 0, 60), off(1.5, 0, 60), off(2.0, 0, 99), note(1.8, 0, 62, 100)};
  const auto plans = cgmusic::muse::plan_channels(sequence, RenderOptions{}, catalog());
  CGMUSIC_CHECK(plans[0].notes.size() == 3);
  const auto& notes = plans[0].notes;
  CGMUSIC_CHECK(notes[0]._location_us == 500'000 && notes[0]._duration_us == 500'000);
  CGMUSIC_CHECK(notes[1]._location_us == 0 && notes[1]._duration_us == 1'500'000);
  CGMUSIC_CHECK(notes[2]._pitch == 62 && notes[2]._duration_us == 200'000);
}

CGMUSIC_TEST(muse_plan_supports_pizzicato_and_tremolo_strings) {
  MidiSequence sequence;
  sequence.duration_s = 1.0;
  sequence.events = {{0.0, 0xC0, 45, 0}, note(0.0, 0, 60, 100), off(1.0, 0, 60),
                     {0.0, 0xC1, 44, 0}, note(0.0, 1, 64, 100), off(1.0, 1, 64)};
  const auto plans = cgmusic::muse::plan_channels(sequence, RenderOptions{}, catalog());
  CGMUSIC_CHECK(plans.size() == 2);
  CGMUSIC_CHECK(plans[0].instrument_id == 91);
  CGMUSIC_CHECK(plans[0].notes[0]._articulation == cgmusic::muse::abi::kArticulationPizzicato);
  CGMUSIC_CHECK(plans[1].instrument_id == 91);
  CGMUSIC_CHECK(plans[1].notes[0]._articulation == cgmusic::muse::abi::kArticulationNone);
}

CGMUSIC_TEST(muse_plan_rejects_unmapped_or_empty_score_without_fallback) {
  MidiSequence sequence;
  sequence.duration_s = 1.0;
  sequence.events = {note(0.0, 0, 60, 100)};
  CGMUSIC_CHECK(error_code_of([&] { cgmusic::muse::plan_channels(sequence, RenderOptions{}, {}); }) ==
                "muse_unavailable");
  sequence.events.clear();
  CGMUSIC_CHECK(error_code_of([&] {
    cgmusic::muse::plan_channels(sequence, RenderOptions{}, catalog());
  }) == "muse_unavailable");
}

namespace {

struct FakeState {
  std::vector<std::string> calls;
  std::vector<cgmusic::muse::abi::ms_NoteEvent_5> notes;
  std::vector<cgmusic::muse::abi::ms_DynamicsEvent_2> dynamics;
  std::size_t rendered = 0;
  bool offline = false;
};

FakeState* g_fake_state = nullptr;

cgmusic::muse::Api fake_api(FakeState& state) {
  g_fake_state = &state;
  using namespace cgmusic::muse::abi;
  using cgmusic::muse::Api;
  Api api{};
  api.ms_MuseSampler_create = []() -> ms_MuseSampler { return reinterpret_cast<ms_MuseSampler>(1); };
  api.ms_MuseSampler_destroy = [](ms_MuseSampler) {};
  api.ms_MuseSampler_init_2 = [](ms_MuseSampler, double rate, int block, int channels) -> ms_Result {
    return rate == 44100.0 && block == 512 && channels == 2 ? kResultOk : kResultError;
  };
  api.ms_MuseSampler_add_track = [](ms_MuseSampler, int id) -> ms_Track {
    g_fake_state->calls.push_back("track:" + std::to_string(id));
    return reinterpret_cast<ms_Track>(id);
  };
  api.ms_MuseSampler_add_track_dynamics_event_2 = [](ms_MuseSampler, ms_Track,
                                                    ms_DynamicsEvent_2 event) -> ms_Result {
    g_fake_state->calls.push_back("dynamics");
    g_fake_state->dynamics.push_back(event);
    return kResultOk;
  };
  api.ms_MuseSampler_add_track_note_event_6 = [](ms_MuseSampler, ms_Track, ms_NoteEvent_5 event,
                                                long long* id) -> ms_Result {
    g_fake_state->calls.push_back("note");
    g_fake_state->notes.push_back(event);
    *id = static_cast<long long>(g_fake_state->notes.size());
    return kResultOk;
  };
  api.ms_MuseSampler_finalize_track = [](ms_MuseSampler, ms_Track) -> ms_Result {
    g_fake_state->calls.push_back("finalize");
    return kResultOk;
  };
  api.ms_MuseSampler_trigger_render = [](ms_MuseSampler) { g_fake_state->calls.push_back("trigger"); };
  api.ms_MuseSampler_ready_to_play = [](ms_MuseSampler) { return true; };
  api.ms_MuseSampler_start_offline_mode = [](ms_MuseSampler, double rate) -> ms_Result {
    g_fake_state->calls.push_back("start");
    g_fake_state->offline = rate == 44100.0;
    return kResultOk;
  };
  api.ms_MuseSampler_process_offline = [](ms_MuseSampler, ms_OutputBuffer output) -> ms_Result {
    g_fake_state->calls.push_back("process");
    if (!g_fake_state->offline || output._num_data_pts != 512 || output._num_channels != 2) return kResultError;
    for (int channel = 0; channel < 2; ++channel) {
      for (int n = 0; n < output._num_data_pts; ++n) {
        output._channels[channel][n] = channel == 0 ? 0.25F : -0.5F;
      }
    }
    g_fake_state->rendered += 512;
    return kResultOk;
  };
  api.ms_MuseSampler_stop_offline_mode = [](ms_MuseSampler) -> ms_Result {
    g_fake_state->calls.push_back("stop");
    g_fake_state->offline = false;
    return kResultOk;
  };
  return api;
}

}  // namespace

CGMUSIC_TEST(muse_offline_session_has_expected_order_and_partial_block) {
  FakeState state;
  cgmusic::muse::Api api = fake_api(state);
  ChannelPlan channel;
  channel.channel = 0;
  channel.instrument_id = 118;
  channel.notes.resize(2);
  channel.notes[0]._pitch = 60;
  channel.notes[1]._pitch = 64;
  channel.dynamics.push_back({0, 0.75});
  Audio audio;
  {
    cgmusic::muse::SamplerSession session(api);
    session.add_score({channel});
    audio = session.render(513);
  }
  const std::vector<std::string> prefix = {"track:118", "dynamics", "note", "note", "finalize",
                                           "trigger", "start", "process", "process"};
  CGMUSIC_CHECK(std::equal(prefix.begin(), prefix.end(), state.calls.begin()));
  CGMUSIC_CHECK(state.calls.back() == "stop");
  CGMUSIC_CHECK(state.notes[0]._pitch == 60 && state.notes[1]._pitch == 64);
  CGMUSIC_CHECK(state.dynamics[0]._value == 0.75);
  CGMUSIC_CHECK(state.rendered == 1024);
  CGMUSIC_CHECK(audio.frames() == 513);
  CGMUSIC_CHECK(audio.channels[0][512] == 0.25F && audio.channels[1][512] == -0.5F);
  CGMUSIC_CHECK(!state.offline);
}

CGMUSIC_TEST(muse_mix_uses_each_stem_once_and_rejects_silence) {
  ChannelPlan first;
  first.channel = 0;
  first.instrument_id = 110;
  first.instrument_name = "Trumpet";
  first.family = "Brass";
  ChannelPlan second = first;
  second.channel = 1;
  second.instrument_id = 118;
  second.instrument_name = "Flute 1";
  second.family = "Woodwinds";
  cgmusic::muse::DryRender dry;
  dry.stems.resize(2);
  for (cgmusic::Audio& stem : dry.stems) {
    stem.rate = cgmusic::kSampleRate;
    stem.channels.assign(2, std::vector<float>(cgmusic::kSampleRate, 0.0F));
  }
  dry.stems[0].channels[0][0] = 0.5F;
  dry.stems[1].channels[1][0] = 0.25F;
  cgmusic::MuseRenderResult result;
  cgmusic::RenderOptions options;
  options.reverb.level = 0.0;
  const Audio audio = cgmusic::muse::mix_rendered(dry, {first, second}, options, -14.0, &result);
  CGMUSIC_CHECK(audio.frames() == cgmusic::kSampleRate);
  CGMUSIC_CHECK(result.mix_report.find("\"channel\":1") != std::string::npos);
  CGMUSIC_CHECK(result.mix_report.find("\"channel\":2") != std::string::npos);
  CGMUSIC_CHECK(result.mix_report.find("\"loudness\"") != std::string::npos);
  dry.stems.clear();
  CGMUSIC_CHECK(error_code_of([&] { cgmusic::muse::mix_rendered(dry, {first}, options, -14.0, &result); }) ==
                "muse_unavailable");
  dry.stems.resize(1);
  dry.stems[0].rate = cgmusic::kSampleRate;
  dry.stems[0].channels.assign(2, std::vector<float>(cgmusic::kSampleRate, 0.0F));
  CGMUSIC_CHECK(error_code_of([&] { cgmusic::muse::mix_rendered(dry, {first}, options, -14.0, &result); }) ==
                "muse_unavailable");
}

CGMUSIC_TEST(muse_process_error_stops_instead_of_returning_audio) {
  FakeState state;
  cgmusic::muse::Api api = fake_api(state);
  api.ms_MuseSampler_process_offline = [](cgmusic::muse::abi::ms_MuseSampler,
                                          cgmusic::muse::abi::ms_OutputBuffer) {
    g_fake_state->calls.push_back("process");
    return -1;
  };
  ChannelPlan channel;
  channel.instrument_id = 118;
  channel.notes.resize(1);
  cgmusic::muse::SamplerSession session(api);
  session.add_score({channel});
  CGMUSIC_CHECK(error_code_of([&] { session.render(512); }) == "muse_unavailable");
  CGMUSIC_CHECK(state.calls.back() == "process");
}

CGMUSIC_TEST(muse_all_fallback_triggers_muse_unavailable_error) {
  MidiSequence sequence;
  sequence.duration_s = 1.0;
  sequence.events = {{0.0, 0xC0, 48, 0}, note(0.0, 0, 60, 100), off(1.0, 0, 60)};
  // catalog 中只有 Flute 118,没有弦乐 91
  Catalog woodwinds_only = {{118, "Flute 1", "Fl.", "Muse Woodwinds", "woodwinds"}};
  CGMUSIC_CHECK(error_code_of([&] {
    cgmusic::muse::plan_channels(sequence, RenderOptions{}, woodwinds_only);
  }) == "muse_unavailable");
}

CGMUSIC_TEST(muse_partial_fallback_plans_and_hybrid_mixes_channels) {
  MidiSequence sequence;
  sequence.duration_s = 1.0;
  sequence.events = {
      {0.0, 0xC0, 73, 0}, note(0.0, 0, 72, 80), off(1.0, 0, 72),
      {0.0, 0xC1, 48, 0}, note(0.0, 1, 60, 90), off(1.0, 1, 60),
  };
  Catalog woodwinds_only = {{118, "Flute 1", "Fl.", "Muse Woodwinds", "woodwinds"}};
  const auto plans = cgmusic::muse::plan_channels(sequence, RenderOptions{}, woodwinds_only);
  CGMUSIC_CHECK(plans.size() == 2);
  // 通道 0 (Flute) 具有 Muse 乐器
  CGMUSIC_CHECK(plans[0].channel == 0);
  CGMUSIC_CHECK(plans[0].instrument_id.has_value());
  CGMUSIC_CHECK(*plans[0].instrument_id == 118);
  CGMUSIC_CHECK(plans[0].family == "woodwinds");
  // 通道 1 (Strings) 需回退到 FluidSynth
  CGMUSIC_CHECK(plans[1].channel == 1);
  CGMUSIC_CHECK(!plans[1].instrument_id.has_value());
  CGMUSIC_CHECK(plans[1].family == "strings");

  // 验证 hybrid 混合
  cgmusic::muse::DryRender dry;
  dry.stems.resize(1);
  dry.stems[0].rate = cgmusic::kSampleRate;
  dry.stems[0].channels.assign(2, std::vector<float>(cgmusic::kSampleRate, 0.2F));

  std::vector<cgmusic::Audio> fallback_stems(1);
  fallback_stems[0].rate = cgmusic::kSampleRate;
  fallback_stems[0].channels.assign(2, std::vector<float>(cgmusic::kSampleRate, 0.3F));

  cgmusic::MuseRenderResult report;
  // 记录回退条目
  report.fallbacks.push_back({plans[1].channel + 1, cgmusic::PresetKey{0, plans[1].program},
                              "muse_instrument_not_licensed"});
  cgmusic::RenderOptions options;
  options.reverb.level = 0.0;
  const Audio mixed = cgmusic::muse::mix_hybrid(dry, {plans[0]}, fallback_stems, {plans[1]},
                                                options, -14.0, &report);
  CGMUSIC_CHECK(mixed.frames() == cgmusic::kSampleRate);
  CGMUSIC_CHECK(report.tracks.size() == 1);
  CGMUSIC_CHECK(report.tracks[0].channel == 1);
  CGMUSIC_CHECK(report.tracks[0].instrument_id == 118);
  CGMUSIC_CHECK(report.fallbacks.size() == 1);
  CGMUSIC_CHECK(report.fallbacks[0].channel == 2);
  CGMUSIC_CHECK(report.fallbacks[0].wanted.program == 48);
  CGMUSIC_CHECK(report.fallbacks[0].reason == "muse_instrument_not_licensed");
  CGMUSIC_CHECK(report.mix_report.find("\"channel\":1") != std::string::npos);
  CGMUSIC_CHECK(report.mix_report.find("\"channel\":2") != std::string::npos);
}

CGMUSIC_TEST(muse_abi_exception_caught_and_formatted_as_json) {
  FakeState state;
  cgmusic::muse::Api api = fake_api(state);
  api.ms_MuseSampler_add_track = [](cgmusic::muse::abi::ms_MuseSampler, int) -> cgmusic::muse::abi::ms_Track {
    throw std::invalid_argument("Invalid file name");
  };
  api.ms_MuseSampler_destroy = [](cgmusic::muse::abi::ms_MuseSampler) {
    // 模拟析构期间库也抛出异常,不应触发 std::terminate
    throw std::runtime_error("destroy failed during unwind");
  };

  ChannelPlan channel;
  channel.instrument_id = 91;
  channel.notes.resize(1);

  try {
    cgmusic::muse::SamplerSession session(api);
    session.add_score({channel});
    cgmusic::test::check_failed("expected exception", __FILE__, __LINE__);
  } catch (const cgmusic::Error& error) {
    CGMUSIC_CHECK(error.code() == "muse_unavailable");
    const std::string msg = error.what();
    CGMUSIC_CHECK(msg.find("ms_MuseSampler_add_track") != std::string::npos);
    CGMUSIC_CHECK(msg.find("Invalid file name") != std::string::npos);

    const std::string json = cgmusic::format_error_json(error.code(), error.what());
    CGMUSIC_CHECK(json.find("\"error\":\"muse_unavailable\"") != std::string::npos);
    CGMUSIC_CHECK(json.find("\"message\":") != std::string::npos);
    CGMUSIC_CHECK(json.find("Invalid file name") != std::string::npos);
  }
}

CGMUSIC_TEST(muse_part_name_mapping_covers_orchestra_and_fallbacks) {
  const Catalog cat = {
      {91, "Violins 1", "Vlns. 1", "Muse Strings", "strings"},
      {92, "Violins 2", "Vlns. 2", "Muse Strings", "strings"},
      {93, "Violas", "Vlas.", "Muse Strings", "strings"},
      {94, "Cellos", "Vlcs.", "Muse Strings", "strings"},
      {95, "Contrabasses", "Cb.", "Muse Strings", "strings"},
      {108, "French Horn", "Hn", "Muse Brass", "brass"},
      {110, "Trumpet", "Tpt.", "Muse Brass", "brass"},
      {118, "Flute 1", "Fl. 1", "Muse Woodwinds", "woodwinds"},
      {119, "Flute 2", "Fl. 2", "Muse Woodwinds", "woodwinds"},
      {127, "Clarinet in Bb", "Cl.", "Muse Woodwinds", "woodwinds"},
      {128, "Bass Clarinet", "B. Clt.", "Muse Woodwinds", "woodwinds"},
      {133, "Bassoon", "Bsn.", "Muse Woodwinds", "woodwinds"},
      {167, "Piano", "Pno.", "Muse Keys", "keys"},
      {178, "Drum Kit", "Kit", "Muse Percussion", "percussion"},
  };

  // Flute 1 与 Flute 2 (二次出现自动递增)
  CGMUSIC_CHECK(cgmusic::pick_instrument_by_name("Flute", 73, false, cat, 0) == 118);
  CGMUSIC_CHECK(cgmusic::pick_instrument_by_name("Flute", 73, false, cat, 1) == 119);
  // 单簧管与低音单簧管
  CGMUSIC_CHECK(cgmusic::pick_instrument_by_name("Clarinet", 71, false, cat, 0) == 127);
  CGMUSIC_CHECK(cgmusic::pick_instrument_by_name("Bass Clarinet", 71, false, cat, 0) == 128);
  // 大管
  CGMUSIC_CHECK(cgmusic::pick_instrument_by_name("Bassoon", 70, false, cat, 0) == 133);
  // 圆号与小号
  CGMUSIC_CHECK(cgmusic::pick_instrument_by_name("Horn", 60, false, cat, 0) == 108);
  CGMUSIC_CHECK(cgmusic::pick_instrument_by_name("Trumpet", 56, false, cat, 0) == 110);
  // 弦乐全套
  CGMUSIC_CHECK(cgmusic::pick_instrument_by_name("Violins", 48, false, cat, 0) == 91);
  CGMUSIC_CHECK(cgmusic::pick_instrument_by_name("Violins", 48, false, cat, 1) == 92);
  CGMUSIC_CHECK(cgmusic::pick_instrument_by_name("Violas", 48, false, cat, 0) == 93);
  CGMUSIC_CHECK(cgmusic::pick_instrument_by_name("Violoncellos", 48, false, cat, 0) == 94);
  CGMUSIC_CHECK(cgmusic::pick_instrument_by_name("Contrabasses", 48, false, cat, 0) == 95);
  CGMUSIC_CHECK(cgmusic::pick_instrument_by_name("Basses", 48, false, cat, 0) == 95);
  // 打击乐
  CGMUSIC_CHECK(cgmusic::pick_instrument_by_name("Drum Kit", 0, true, cat, 0) == 178);
  CGMUSIC_CHECK(cgmusic::pick_instrument_by_name("Percussion", 48, true, cat, 0) == 178);
  // 钢琴
  CGMUSIC_CHECK(cgmusic::pick_instrument_by_name("Piano", 0, false, cat, 0) == 167);
  // 手风琴回退
  CGMUSIC_CHECK(!cgmusic::pick_instrument_by_name("Accordion", 21, false, cat, 0).has_value());
}

CGMUSIC_TEST(muse_two_parts_sharing_xml_channel_stay_separate) {
  const std::string xml =
      "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
      "<score-partwise version=\"3.1\">\n"
      "  <part-list>\n"
      "    <score-part id=\"P1\">\n"
      "      <part-name>Flute</part-name>\n"
      "      <midi-instrument id=\"P1-I1\">\n"
      "        <midi-channel>1</midi-channel>\n"
      "        <midi-program>74</midi-program>\n"
      "      </midi-instrument>\n"
      "    </score-part>\n"
      "    <score-part id=\"P2\">\n"
      "      <part-name>Violas</part-name>\n"
      "      <midi-instrument id=\"P2-I1\">\n"
      "        <midi-channel>1</midi-channel>\n"
      "        <midi-program>49</midi-program>\n"
      "      </midi-instrument>\n"
      "    </score-part>\n"
      "  </part-list>\n"
      "  <part id=\"P1\">\n"
      "    <measure number=\"1\">\n"
      "      <attributes><divisions>1</divisions></attributes>\n"
      "      <note><pitch><step>C</step><octave>5</octave></pitch><duration>1</duration></note>\n"
      "    </measure>\n"
      "  </part>\n"
      "  <part id=\"P2\">\n"
      "    <measure number=\"1\">\n"
      "      <attributes><divisions>1</divisions></attributes>\n"
      "      <note><pitch><step>G</step><octave>3</octave></pitch><duration>1</duration></note>\n"
      "    </measure>\n"
      "  </part>\n"
      "</score-partwise>\n";
  const std::vector<std::uint8_t> data(xml.begin(), xml.end());
  const MidiSequence sequence = cgmusic::musicxml_to_sequence(data, "two_parts.xml");

  // 验证两声部保持独立虚拟通道
  CGMUSIC_CHECK(sequence.channel_names.size() == 2);
  CGMUSIC_CHECK(sequence.channel_names[0] == "Flute");
  CGMUSIC_CHECK(sequence.channel_names[1] == "Violas");

  const Catalog cat = {
      {93, "Violas", "Vlas.", "Muse Strings", "strings"},
      {118, "Flute 1", "Fl.", "Muse Woodwinds", "woodwinds"},
  };
  const auto plans = cgmusic::muse::plan_channels(sequence, RenderOptions{}, cat);
  CGMUSIC_CHECK(plans.size() == 2);
  CGMUSIC_CHECK(plans[0].instrument_id == 118);
  CGMUSIC_CHECK(plans[0].instrument_name == "Flute 1");
  CGMUSIC_CHECK(plans[1].instrument_id == 93);
  CGMUSIC_CHECK(plans[1].instrument_name == "Violas");

  // 验证 SMF/MIDI 路径正常产出合规字节流
  const auto smf = cgmusic::musicxml_to_midi(data, "two_parts.xml");
  CGMUSIC_CHECK(smf.size() > 14);
  CGMUSIC_CHECK(smf[0] == 'M' && smf[1] == 'T' && smf[2] == 'h' && smf[3] == 'd');
}

CGMUSIC_TEST(muse_synthetic_pizzicato_arco_ranges) {
  const std::string xml =
      "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
      "<score-partwise version=\"3.1\">\n"
      "  <part-list>\n"
      "    <score-part id=\"P1\">\n"
      "      <part-name>Violins</part-name>\n"
      "      <midi-instrument id=\"P1-I1\">\n"
      "        <midi-channel>1</midi-channel>\n"
      "        <midi-program>41</midi-program>\n"
      "      </midi-instrument>\n"
      "    </score-part>\n"
      "  </part-list>\n"
      "  <part id=\"P1\">\n"
      "    <measure number=\"1\">\n"
      "      <attributes><divisions>1</divisions></attributes>\n"
      "      <note><pitch><step>C</step><octave>4</octave></pitch><duration>1</duration></note>\n"
      "    </measure>\n"
      "    <measure number=\"2\">\n"
      "      <direction><direction-type><words>pizz.</words></direction-type></direction>\n"
      "      <note><pitch><step>E</step><octave>4</octave></pitch><duration>1</duration></note>\n"
      "    </measure>\n"
      "    <measure number=\"3\">\n"
      "      <direction><direction-type><words>arco</words></direction-type></direction>\n"
      "      <note><pitch><step>G</step><octave>4</octave></pitch><duration>1</duration></note>\n"
      "    </measure>\n"
      "  </part>\n"
      "</score-partwise>\n";
  const std::vector<std::uint8_t> data(xml.begin(), xml.end());
  const MidiSequence sequence = cgmusic::musicxml_to_sequence(data, "pizz.xml");
  const Catalog cat = {{91, "Violins 1", "Vlns.", "Muse Strings", "strings"}};
  const auto plans = cgmusic::muse::plan_channels(sequence, RenderOptions{}, cat);

  CGMUSIC_CHECK(plans.size() == 1);
  CGMUSIC_CHECK(plans[0].notes.size() == 3);
  // 音符 1: 缺省 arco
  CGMUSIC_CHECK(plans[0].notes[0]._articulation == cgmusic::muse::abi::kArticulationNone);
  // 音符 2: pizz.
  CGMUSIC_CHECK(plans[0].notes[1]._articulation == cgmusic::muse::abi::kArticulationPizzicato);
  // 音符 3: arco 恢复
  CGMUSIC_CHECK(plans[0].notes[2]._articulation == cgmusic::muse::abi::kArticulationNone);
}

CGMUSIC_TEST(muse_drum_trim_only_applied_to_percussion_stems) {
  ChannelPlan brass_plan;
  brass_plan.channel = 0;
  brass_plan.instrument_id = 110;
  brass_plan.instrument_name = "Trumpet";
  brass_plan.family = "brass";
  brass_plan.notes.resize(1);

  ChannelPlan drum_plan;
  drum_plan.channel = 1;
  drum_plan.instrument_id = 178;
  drum_plan.instrument_name = "Drum Kit";
  drum_plan.family = "percussion";
  drum_plan.notes.resize(1);

  cgmusic::muse::DryRender dry;
  dry.stems.resize(2);
  for (cgmusic::Audio& stem : dry.stems) {
    stem.rate = cgmusic::kSampleRate;
    stem.channels.assign(2, std::vector<float>(cgmusic::kSampleRate, 0.5F));
  }

  cgmusic::MuseRenderResult result;
  cgmusic::RenderOptions options;
  options.reverb.level = 0.0;
  options.balance = false;  // 关闭自动平衡以便直接检验衰减
  cgmusic::muse::mix_hybrid(dry, {brass_plan, drum_plan}, {}, {}, options, -14.0, &result);

  CGMUSIC_CHECK(result.tracks.size() == 2);
  // 验证 RMS 暴露在报告中 (dBFS 应约为 20*log10(0.5) = -6.02 dBFS)
  CGMUSIC_CHECK(near(result.tracks[0].rms_dbfs, -6.02, 0.1));
  CGMUSIC_CHECK(near(result.tracks[1].rms_dbfs, -6.02, 0.1));
  // 混音报告中的增益衰减包含 -12 dB 衰减
  CGMUSIC_CHECK(result.tracks[1].family == "percussion");
}
