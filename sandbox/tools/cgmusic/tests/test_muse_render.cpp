#include <algorithm>
#include <cmath>
#include <cstddef>
#include <functional>
#include <string>
#include <vector>

#include "common.hpp"
#include "muse/render.hpp"
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
