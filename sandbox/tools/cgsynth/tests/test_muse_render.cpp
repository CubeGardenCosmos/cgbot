#include <algorithm>
#include <cmath>
#include <functional>
#include <string>
#include <vector>

#include "muse/mapping.hpp"
#include "muse/render.hpp"
#include "test_harness.hpp"

namespace {
using cgmusic::Catalog;
using cgmusic::MidiEvent;
using cgmusic::MidiSequence;
using cgmusic::muse::ChannelPlan;

Catalog catalog() {
  return {{91, "Violins 1", "Vlns.", "Muse Strings", "strings"},
          {110, "Trumpet", "Tpt.", "Muse Brass", "brass"},
          {118, "Flute 1", "Fl.", "Muse Woodwinds", "woodwinds"}};
}
MidiEvent note(double time, int channel, int pitch, int velocity) {
  return {time, static_cast<std::uint8_t>(0x90 | channel), static_cast<std::uint8_t>(pitch),
          static_cast<std::uint8_t>(velocity)};
}
MidiEvent off(double time, int channel, int pitch) {
  return {time, static_cast<std::uint8_t>(0x80 | channel), static_cast<std::uint8_t>(pitch), 0};
}
std::string error_code_of(const std::function<void()>& action) {
  try { action(); } catch (const cgmusic::Error& error) { return error.code(); }
  return "none";
}
bool near(double a, double b) { return std::fabs(a - b) < 1e-6; }
}  // namespace

CGMUSIC_TEST(muse_engine_parser_accepts_only_exact_names) {
  CGMUSIC_CHECK(cgmusic::parse_render_engine("fluidsynth") == cgmusic::RenderEngine::FluidSynth);
  CGMUSIC_CHECK(cgmusic::parse_render_engine("muse") == cgmusic::RenderEngine::Muse);
  CGMUSIC_CHECK(error_code_of([] { cgmusic::parse_render_engine("Muse"); }) == "bad_input");
}

CGMUSIC_TEST(muse_plan_pairs_notes_and_applies_tempo_and_velocity) {
  MidiSequence sequence;
  sequence.duration_s = 2.0;
  sequence.tempo_map = {{0, 0.0, 500'000}, {480, 1.0, 250'000}};
  sequence.events = {{0.0, 0xC0, 73, 0}, note(0.0, 0, 72, 70), off(0.5, 0, 72),
                     note(1.5, 0, 74, 100), off(2.0, 0, 74)};
  const auto plans = cgmusic::muse::plan_channels(sequence, catalog());
  CGMUSIC_CHECK(plans.size() == 1 && plans[0].instrument_id == 118);
  CGMUSIC_CHECK(plans[0].notes.size() == 2);
  CGMUSIC_CHECK(plans[0].notes[0]._duration_us == 500'000);
  CGMUSIC_CHECK(near(plans[0].notes[0]._tempo, 120.0));
  CGMUSIC_CHECK(plans[0].notes[1]._location_us == 1'500'000);
  CGMUSIC_CHECK(near(plans[0].notes[1]._tempo, 240.0));
  CGMUSIC_CHECK(near(plans[0].dynamics[0]._value, 100.0 / 127.0));
}

CGMUSIC_TEST(muse_plan_closes_unfinished_and_overlapping_notes) {
  MidiSequence sequence;
  sequence.duration_s = 2.0;
  sequence.events = {{0.0, 0xC0, 73, 0}, note(0.0, 0, 60, 80), note(0.5, 0, 60, 90),
                     off(1.0, 0, 60), off(1.5, 0, 60), off(2.0, 0, 99), note(1.8, 0, 62, 100)};
  const auto plans = cgmusic::muse::plan_channels(sequence, catalog());
  CGMUSIC_CHECK(plans[0].notes.size() == 3);
  CGMUSIC_CHECK(plans[0].notes[0]._location_us == 500'000);
  CGMUSIC_CHECK(plans[0].notes[1]._location_us == 0);
  CGMUSIC_CHECK(plans[0].notes[2]._duration_us == 200'000);
}

CGMUSIC_TEST(muse_plan_maps_pizzicato_from_gm_program) {
  MidiSequence sequence;
  sequence.duration_s = 1.0;
  sequence.events = {{0.0, 0xC0, 45, 0}, note(0.0, 0, 60, 100), off(1.0, 0, 60)};
  const auto plans = cgmusic::muse::plan_channels(sequence, catalog());
  CGMUSIC_CHECK(plans[0].notes[0]._articulation == cgmusic::muse::abi::kArticulationPizzicato);
}

CGMUSIC_TEST(muse_track_name_is_exact_not_guessed) {
  MidiSequence sequence;
  sequence.duration_s = 1.0;
  sequence.channel_names = {"Violins 2"};
  sequence.events = {{0.0, 0xC0, 48, 0}, note(0.0, 0, 60, 100), off(1.0, 0, 60)};
  const auto plans = cgmusic::muse::plan_channels(sequence, catalog());
  // 不允许把近似名称猜成 Violins 1；未精确命中时走 GM 映射。
  CGMUSIC_CHECK(plans[0].instrument_id == 91);
  sequence.channel_names = {"Flute 1"};
  const auto exact = cgmusic::muse::plan_channels(sequence, catalog());
  CGMUSIC_CHECK(exact[0].instrument_id == 118);
}

CGMUSIC_TEST(muse_empty_score_has_no_plan) {
  CGMUSIC_CHECK(error_code_of([] { cgmusic::muse::plan_channels({}, catalog()); }) ==
                "muse_unavailable");
}

namespace {
struct FakeState {
  std::vector<std::string> calls;
  std::size_t rendered = 0;
  bool offline = false;
};
FakeState* g_fake_state = nullptr;
cgmusic::muse::Api fake_api(FakeState& state) {
  g_fake_state = &state;
  using namespace cgmusic::muse::abi;
  cgmusic::muse::Api api{};
  api.ms_MuseSampler_create = []() -> ms_MuseSampler { return reinterpret_cast<ms_MuseSampler>(1); };
  api.ms_MuseSampler_destroy = [](ms_MuseSampler) {};
  api.ms_MuseSampler_init_2 = [](ms_MuseSampler, double rate, int block, int channels) -> ms_Result {
    return rate == 44100.0 && block == 512 && channels == 2 ? kResultOk : kResultError;
  };
  api.ms_MuseSampler_add_track = [](ms_MuseSampler, int) -> ms_Track {
    return reinterpret_cast<ms_Track>(1);
  };
  api.ms_MuseSampler_add_track_dynamics_event_2 = [](ms_MuseSampler, ms_Track,
                                                    ms_DynamicsEvent_2) -> ms_Result {
    g_fake_state->calls.push_back("dynamics"); return kResultOk;
  };
  api.ms_MuseSampler_add_track_note_event_6 = [](ms_MuseSampler, ms_Track, ms_NoteEvent_5,
                                                long long* id) -> ms_Result {
    g_fake_state->calls.push_back("note"); *id = 1; return kResultOk;
  };
  api.ms_MuseSampler_finalize_track = [](ms_MuseSampler, ms_Track) -> ms_Result {
    g_fake_state->calls.push_back("finalize"); return kResultOk;
  };
  api.ms_MuseSampler_trigger_render = [](ms_MuseSampler) {};
  api.ms_MuseSampler_ready_to_play = [](ms_MuseSampler) { return true; };
  api.ms_MuseSampler_start_offline_mode = [](ms_MuseSampler, double) -> ms_Result {
    g_fake_state->offline = true; return kResultOk;
  };
  api.ms_MuseSampler_process_offline = [](ms_MuseSampler, ms_OutputBuffer output) -> ms_Result {
    g_fake_state->calls.push_back("process");
    if (!g_fake_state->offline || output._num_data_pts != 512) return kResultError;
    std::fill_n(output._channels[0], output._num_data_pts, 0.25F);
    std::fill_n(output._channels[1], output._num_data_pts, -0.5F);
    g_fake_state->rendered += 512;
    return kResultOk;
  };
  api.ms_MuseSampler_stop_offline_mode = [](ms_MuseSampler) -> ms_Result {
    g_fake_state->offline = false; g_fake_state->calls.push_back("stop"); return kResultOk;
  };
  return api;
}
}  // namespace

CGMUSIC_TEST(muse_offline_session_preserves_dry_float_and_partial_block) {
  FakeState state;
  const auto api = fake_api(state);
  ChannelPlan channel;
  channel.instrument_id = 118;
  channel.notes.resize(1);
  channel.dynamics.push_back({0, 0.75});
  cgmusic::muse::SamplerSession session(api);
  session.add_score({channel});
  const auto audio = session.render(513);
  CGMUSIC_CHECK(audio.frames() == 513 && state.rendered == 1024);
  CGMUSIC_CHECK(audio.channels[0][512] == 0.25F && audio.channels[1][512] == -0.5F);
  CGMUSIC_CHECK(!state.offline);
}

CGMUSIC_TEST(muse_process_error_is_structured) {
  FakeState state;
  auto api = fake_api(state);
  api.ms_MuseSampler_process_offline = [](cgmusic::muse::abi::ms_MuseSampler,
                                          cgmusic::muse::abi::ms_OutputBuffer) { return -1; };
  ChannelPlan channel;
  channel.instrument_id = 118;
  channel.notes.resize(1);
  cgmusic::muse::SamplerSession session(api);
  session.add_score({channel});
  CGMUSIC_CHECK(error_code_of([&] { session.render(512); }) == "muse_unavailable");
}

CGMUSIC_TEST(muse_abi_exception_becomes_json_error) {
  FakeState state;
  auto api = fake_api(state);
  api.ms_MuseSampler_add_track = [](cgmusic::muse::abi::ms_MuseSampler,
                                    int) -> cgmusic::muse::abi::ms_Track {
    throw std::invalid_argument("Invalid file name");
  };
  ChannelPlan channel;
  channel.instrument_id = 118;
  channel.notes.resize(1);
  cgmusic::muse::SamplerSession session(api);
  CGMUSIC_CHECK(error_code_of([&] { session.add_score({channel}); }) == "muse_unavailable");
}
