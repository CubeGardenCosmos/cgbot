"""hands、bars、pedal 的离线测试。"""

from __future__ import annotations

import pytest
from midi_helpers import absolute, build_midi, control, note_off, note_on, program, tempo
from mido import MetaMessage

from cgmidi.hands import merge_hands_midi
from cgmidi.score import analyze_bars, apply_pedal


def _notes(mid, track=0):
    return [
        (tick, msg.type, msg.note, msg.velocity)
        for tick, msg in absolute(mid.tracks[track])
        if msg.type in ("note_on", "note_off")
    ]


def _cc64(mid):
    return [
        (tick, msg.channel, msg.value)
        for track in mid.tracks
        for tick, msg in absolute(track)
        if msg.type == "control_change" and msg.control == 64
    ]


def _piano_tracks():
    right = [program(0, 0, 0)]
    left = []
    for tick in (0, 480, 960, 1440, 1920):
        right.extend((note_on(tick, 0, 72, 40), note_off(tick + 240, 0, 72)))
        left.extend((note_on(tick, 0, 48, 60), note_off(tick + 240, 0, 48)))
    return [right, left]


def test_hands_merges_baselines_and_inherits_channel_program():
    mid = build_midi(_piano_tracks())
    out, report = merge_hands_midi(mid, window_beats=2)
    assert report["channels_merged"] == [1]
    assert report["notes_changed"] == 5
    assert [msg.velocity for _tick, msg in absolute(out.tracks[0]) if msg.type == "note_on"] == [
        60
    ] * 5
    assert _notes(out, 1) == _notes(mid, 1)


def test_hands_preserves_positive_and_negative_accents():
    right, left = _piano_tracks()
    right[2] = note_on(480, 0, 72, 56)  # own baseline 40, offset +16
    left[4] = note_on(960, 0, 48, 76)  # own baseline 60, offset +16
    out, report = merge_hands_midi(build_midi([right, left]), window_beats=2)
    right_vel = [msg.velocity for _tick, msg in absolute(out.tracks[0]) if msg.type == "note_on"]
    left_vel = [msg.velocity for _tick, msg in absolute(out.tracks[1]) if msg.type == "note_on"]
    assert right_vel[1] == 76
    assert left_vel[2] == 76
    assert report["accents_kept"] == 2


def test_hands_skips_drums_different_programs_and_single_tracks():
    drums = [
        [note_on(0, 9, 36, 40), note_off(480, 9, 36)],
        [note_on(0, 9, 38, 80), note_off(480, 9, 38)],
    ]
    out, report = merge_hands_midi(build_midi(drums))
    assert report["notes_changed"] == 0
    assert _notes(out) == _notes(build_midi(drums))

    tracks = _piano_tracks()
    tracks[1].insert(0, program(0, 0, 40))
    _out, report = merge_hands_midi(build_midi(tracks))
    assert report["channels_merged"] == []


def test_hands_channel_filter_and_empty_result():
    tracks = _piano_tracks()
    out, report = merge_hands_midi(build_midi(tracks), channels={1})
    assert report["channels_merged"] == []
    assert _notes(out) == _notes(build_midi(tracks))


def test_bars_tempo_time_signature_and_pickup():
    events = [
        tempo(0, 500_000),
        (0, MetaMessage("time_signature", numerator=2, denominator=4)),
        note_on(0, 0, 60),
        note_off(480, 0, 60),
        (480, MetaMessage("time_signature", numerator=3, denominator=4)),
        note_on(480, 0, 62),
        note_off(1920, 0, 62),
        tempo(1920, 250_000),
        note_on(1920, 0, 64),
        note_off(3360, 0, 64),
    ]
    result = analyze_bars(build_midi([events]))
    assert result["total_bars"] == 3
    assert [
        (bar["start_tick"], bar["end_tick"], bar["time_signature"], bar["pickup"])
        for bar in result["bars"]
    ] == [
        (0, 480, "2/4", True),
        (480, 1920, "3/4", False),
        (1920, 3360, "3/4", False),
    ]
    assert result["bars"][1]["end_seconds"] == pytest.approx(2.0)
    assert result["bars"][2]["end_seconds"] == pytest.approx(2.75)


def test_bars_pickup_from_short_initial_bar():
    events = [
        (0, MetaMessage("time_signature", numerator=1, denominator=4)),
        note_on(0, 0, 60),
        note_off(480, 0, 60),
        (480, MetaMessage("time_signature", numerator=4, denominator=4)),
        note_on(480, 0, 64),
        note_off(2400, 0, 64),
    ]
    result = analyze_bars(build_midi([events]))
    assert result["bars"][0]["pickup"] is True
    assert result["bars"][1]["start_tick"] == 480


def test_bars_harmony_collects_arpeggio_pitch_classes():
    events = [tempo(0, 500_000)]
    for index, pitch in enumerate((48, 55, 60, 64)):
        events.extend((note_on(index * 480, 0, pitch, 70), note_off(index * 480 + 400, 0, pitch)))
    result = analyze_bars(build_midi([events]))
    harmony = result["bars"][0]["harmony"]
    assert len(harmony) == 1
    assert harmony[0]["pitch_classes"] == [0, 4, 7]
    assert harmony[0]["pitch_class_names"] == ["C", "E", "G"]
    assert harmony[0]["chord"] == "C"


def test_bars_channel_filter_excludes_notes():
    events = [
        note_on(0, 0, 60),
        note_off(1920, 0, 60),
        note_on(0, 1, 65),
        note_off(1920, 1, 65),
    ]
    result = analyze_bars(build_midi([events]), channels={0})
    assert result["total_notes"] == 1
    assert result["channels"] == [1]
    assert result["bars"][0]["harmony"][0]["pitch_classes"] == [0]


def test_pedal_bar_releases_before_and_presses_after_boundaries():
    events = [
        note_on(0, 0, 60),
        note_off(1920, 0, 60),
        note_on(1920, 0, 64),
        note_off(3840, 0, 64),
    ]
    mid = build_midi([events])
    out, report = apply_pedal(mid, mode="bar", channels={0}, gap_ticks=30)
    assert out is not None
    assert _cc64(out) == [(30, 0, 127), (1890, 0, 0), (1950, 0, 127), (3840, 0, 0)]
    assert report["notes_changed"] == report["velocities_changed"] == 0
    assert _notes(out) == _notes(mid)


def test_pedal_chord_changes_at_harmony_segments():
    events = []
    for bar, pitches in enumerate(((48, 55, 60, 64), (53, 57, 60, 65))):
        for index, pitch in enumerate(pitches):
            tick = bar * 1920 + index * 480
            events.extend((note_on(tick, 0, pitch, 70), note_off(tick + 400, 0, pitch)))
    out, _report = apply_pedal(build_midi([events]), mode="chord", channels={0})
    assert out is not None
    changes = [tick for tick, _channel, value in _cc64(out) if value == 127]
    assert changes == [30, 1950]


def test_pedal_auto_uses_chord_for_arpeggio_and_bar_for_block_chords():
    events = []
    for index, pitch in enumerate((48, 55, 60, 64)):
        events.extend((note_on(index * 480, 0, pitch), note_off(index * 480 + 400, 0, pitch)))
    for pitch in (53, 60, 65):
        events.extend((note_on(1920, 0, pitch), note_off(3840, 0, pitch)))
    out, report = apply_pedal(build_midi([events]), mode="auto", channels={0})
    assert out is not None
    assert report["mode"] == "auto"
    assert report["resolved_mode"] == "bar"
    assert [tick for tick, _channel, value in _cc64(out) if value == 127] == [30, 1950]


def test_pedal_existing_cc64_respected_unless_replaced():
    events = [
        note_on(0, 0, 60),
        control(100, 0, 64, 127),
        control(1800, 0, 64, 0),
        note_off(1920, 0, 60),
    ]
    mid = build_midi([events])
    out, report = apply_pedal(mid, channels={0})
    assert out is None
    assert report["status"] == "already_pedaled"
    assert report["existing_pedal_events"] == 2

    out, report = apply_pedal(mid, channels={0}, replace=True)
    assert out is not None
    assert report["removed_pedal_events"] == 2
    assert _cc64(out) != _cc64(mid)
    assert _notes(out) == _notes(mid)


def test_pedal_does_not_modify_unselected_channel_cc64():
    events = [note_on(0, 0, 60), note_off(1920, 0, 60), control(0, 1, 64, 100)]
    out, _report = apply_pedal(build_midi([events]), channels={0})
    assert out is not None
    assert (0, 1, 100) in _cc64(out)
