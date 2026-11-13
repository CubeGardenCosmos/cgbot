"""无音符声部的跳过逻辑:cgscore 会为空通道生成只含元数据的 MIDI,cgsynth 对其报错。"""

from pathlib import Path

import mido
import pytest

from cgmusic import pipeline
from cgmusic.errors import BAD_INPUT, CgmusicError


def _write_midi(path: Path, *, notes: bool) -> None:
    mid = mido.MidiFile(ticks_per_beat=480)
    track = mido.MidiTrack()
    mid.tracks.append(track)
    track.append(mido.MetaMessage("set_tempo", tempo=500000, time=0))
    track.append(mido.Message("program_change", program=0, channel=0, time=0))
    if notes:
        track.append(mido.Message("note_on", note=60, velocity=80, channel=0, time=0))
        track.append(mido.Message("note_off", note=60, velocity=0, channel=0, time=480))
    track.append(mido.MetaMessage("end_of_track", time=0))
    mid.save(str(path))


def test_sounding_parts_drops_parts_without_notes(tmp_path: Path) -> None:
    _write_midi(tmp_path / "a.mid", notes=True)
    _write_midi(tmp_path / "b.mid", notes=False)
    parts = [{"id": "A", "file": "a.mid"}, {"id": "B", "file": "b.mid"}]

    kept = pipeline._sounding_parts(tmp_path, parts)

    assert [p["id"] for p in kept] == ["A"]


def test_all_silent_parts_is_bad_input(tmp_path: Path) -> None:
    _write_midi(tmp_path / "b.mid", notes=False)
    parts = [{"id": "B", "file": "b.mid"}]

    with pytest.raises(CgmusicError) as err:
        pipeline._sounding_parts(tmp_path, parts)

    assert err.value.code == BAD_INPUT
