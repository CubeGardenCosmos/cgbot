"""cgmidi.ops 的离线单元测试。所有期望值按 mido 时间模型手算。"""

from __future__ import annotations

import pytest
from midi_helpers import (
    absolute,
    build_midi,
    note_off,
    note_on,
    notes_of,
    program,
    tempo,
)
from mido import bpm2tempo, tempo2bpm

from cgmidi import ops
from cgmidi.errors import BAD_INPUT, OUT_OF_RANGE, CgmidiError


def _tempos(events):
    return [(tick, msg.tempo) for tick, msg in events if msg.type == "set_tempo"]


def _programs(events):
    return [(tick, msg.channel, msg.program) for tick, msg in events
            if msg.type == "program_change"]


def _sounding(events):
    return sorted({(msg.channel, msg.note) for _, msg in events if msg.type == "note_on"})


def test_speed_scales_every_tempo_marker():
    mid = build_midi([[tempo(0, 500_000), tempo(960, 1_000_000)]])
    out = ops.transform(mid, ops.Options(speed=2.0))
    assert _tempos(absolute(out.tracks[0])) == [(0, 250_000), (960, 500_000)]


def test_speed_inserts_default_tempo_when_none_marked():
    mid = build_midi([[note_on(0, 0, 60), note_off(480, 0, 60)]])
    out = ops.transform(mid, ops.Options(speed=2.0))
    assert _tempos(absolute(out.tracks[0])) == [(0, 250_000)]


def test_bpm_replaces_all_tempo_markers():
    mid = build_midi([[tempo(0, 500_000), tempo(480, 1_000_000), note_on(0, 0, 60),
                       note_off(480, 0, 60)]])
    out = ops.transform(mid, ops.Options(bpm=90))
    tempos = _tempos(absolute(out.tracks[0]))
    assert tempos == [(0, bpm2tempo(90))]
    assert tempo2bpm(tempos[0][1]) == pytest.approx(90, abs=0.01)


def test_transpose_skips_drum_channel():
    mid = build_midi([[note_on(0, 0, 60), note_off(480, 0, 60),
                       note_on(0, 9, 60), note_off(480, 9, 60)]])
    out = ops.transform(mid, ops.Options(transpose=2))
    assert sorted(notes_of(absolute(out.tracks[0]))) == sorted([
        (0, "note_on", 0, 62),
        (0, "note_on", 9, 60),
        (480, "note_off", 0, 62),
        (480, "note_off", 9, 60),
    ])


def test_transpose_out_of_range_raises():
    mid = build_midi([[note_on(0, 0, 127), note_off(480, 0, 127)]])
    with pytest.raises(CgmidiError) as exc:
        ops.transform(mid, ops.Options(transpose=1))
    assert exc.value.code == OUT_OF_RANGE


@pytest.fixture
def two_channels():
    return build_midi([[note_on(0, 0, 60), note_off(480, 0, 60),
                        note_on(0, 1, 64), note_off(480, 1, 64)]])


def test_mute_drops_channel(two_channels):
    out = ops.transform(two_channels, ops.Options(mute={0}))
    assert _sounding(absolute(out.tracks[0])) == [(1, 64)]


def test_solo_keeps_only_listed_channel(two_channels):
    out = ops.transform(two_channels, ops.Options(solo={0}))
    assert _sounding(absolute(out.tracks[0])) == [(0, 60)]


def test_solo_and_mute_on_same_channel_leaves_nothing(two_channels):
    out = ops.transform(two_channels, ops.Options(solo={0}, mute={0}))
    assert _sounding(absolute(out.tracks[0])) == []


def test_map_channel_moves_notes_and_program():
    mid = build_midi([[program(0, 1, 5), note_on(0, 1, 64), note_off(480, 1, 64)]])
    out = ops.transform(mid, ops.Options(map_channel={1: 3}))
    events = absolute(out.tracks[0])
    assert _programs(events) == [(0, 3, 5)]
    assert notes_of(events) == [(0, "note_on", 3, 64), (480, "note_off", 3, 64)]


def test_set_program_replaces_every_existing_program_change():
    mid = build_midi([[program(0, 0, 1), program(480, 0, 2), program(0, 1, 7)]])
    out = ops.transform(mid, ops.Options(set_program={0: 40}))
    events = absolute(out.tracks[0])
    assert [(tick, prog) for tick, ch, prog in _programs(events) if ch == 0] == [(0, 40)]
    assert sorted((ch, prog) for _, ch, prog in _programs(events)) == [(0, 40), (1, 7)]


def test_set_bank_precedes_set_program_at_tick_zero():
    mid = build_midi([[note_on(0, 0, 60), note_off(480, 0, 60)]])
    out = ops.transform(mid, ops.Options(set_bank={0: (1, 2)}, set_program={0: 40}))
    head = [
        (msg.type, msg.control, msg.value) if msg.type == "control_change"
        else (msg.type, None, msg.program)
        for tick, msg in absolute(out.tracks[0])
        if tick == 0 and msg.type in ("control_change", "program_change")
    ]
    assert head == [
        ("control_change", 0, 1),
        ("control_change", 32, 2),
        ("program_change", None, 40),
    ]


def test_map_program_applies_to_all_channels():
    mid = build_midi([[program(0, 0, 5), program(0, 1, 5)]])
    out = ops.transform(mid, ops.Options(map_program={5: 6}))
    assert sorted((ch, prog) for _, ch, prog in _programs(absolute(out.tracks[0]))) == [
        (0, 6),
        (1, 6),
    ]


def test_trim_folds_state_and_closes_sounding_notes():
    # 默认速度 500000(每拍 0.5 秒,tpb 480):0.75 秒 = tick 720,1.5 秒 = tick 1440
    mid = build_midi([[program(100, 0, 5), note_on(0, 0, 60), note_off(960, 0, 60),
                       note_on(960, 0, 62), note_off(1440, 0, 62)]])
    out = ops.transform(mid, ops.Options(start=0.75, end=1.5))
    events = absolute(out.tracks[0])
    assert _programs(events) == [(0, 0, 5)]
    assert notes_of(events) == [
        (0, "note_on", 0, 60),
        (240, "note_off", 0, 60),
        (240, "note_on", 0, 62),
        (720, "note_off", 0, 62),
    ]


def test_trim_across_tempo_change_uses_source_timeline():
    # 第一段 0.5 秒(tick 0-480),之后每拍 1 秒;起点 1.0 秒 = tick 720
    mid = build_midi([[tempo(0, 500_000), tempo(480, 1_000_000),
                       note_on(960, 0, 60), note_off(1440, 0, 60)]])
    out = ops.transform(mid, ops.Options(start=1.0))
    events = absolute(out.tracks[0])
    assert notes_of(events) == [(240, "note_on", 0, 60), (720, "note_off", 0, 60)]
    assert _tempos(events)[-1] == (0, 1_000_000)


def test_describe_reports_duration_channels_and_range():
    mid = build_midi([[program(0, 1, 5), tempo(0, 500_000), note_on(0, 1, 60),
                       note_off(480, 1, 60), note_on(480, 1, 64), note_off(960, 1, 64)]])
    summary = ops.describe(mid)
    assert summary["duration_seconds"] == 1.0
    assert summary["notes"] == 2
    assert summary["channels"] == [
        {"channel": 2, "notes": 2, "note_range": [60, 64], "programs": [5], "banks": []}
    ]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"speed": 0.0},
        {"bpm": 0},
        {"speed": 2.0, "bpm": 90},
        {"transpose": 60},
        {"start": 2.0, "end": 1.0},
        {"mute": {16}},
        {"set_program": {0: 128}},
    ],
)
def test_invalid_options_raise_bad_input(kwargs):
    mid = build_midi([[note_on(0, 0, 60), note_off(480, 0, 60)]])
    with pytest.raises(CgmidiError) as exc:
        ops.transform(mid, ops.Options(**kwargs))
    assert exc.value.code == BAD_INPUT


def test_start_beyond_duration_raises_bad_input():
    mid = build_midi([[note_on(0, 0, 60), note_off(480, 0, 60)]])
    with pytest.raises(CgmidiError) as exc:
        ops.transform(mid, ops.Options(start=10.0))
    assert exc.value.code == BAD_INPUT
