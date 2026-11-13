"""参数解析、部件解析与并发数的单元测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from cgmusic import pipeline
from cgmusic.errors import BAD_INPUT, SYNTH_FAILED, CgmusicError, from_cgsynth_stderr
from cgmusic.options import RenderOptions, parse_render_args
from cgmusic.plan import PartTask, resolve_parts, source_channels


def _opts(**kwargs: object) -> RenderOptions:
    return RenderOptions(input=Path("x.mid"), **kwargs)  # type: ignore[arg-type]


def _part(pid: str, channel: object, hint: str = "muse|fluidsynth|null") -> dict:
    return {
        "id": pid,
        "name": pid,
        "file": f"parts/{pid}.mid",
        "source_channel": channel,
        "engine_hint": hint,
        "mix": {},
    }


def _doc(*parts: dict) -> dict:
    return {"duration_s": 1.0, "parts": list(parts)}


def _task(pid: str, engine: str = "fluidsynth") -> PartTask:
    return PartTask(pid, pid, f"parts/{pid}.mid", engine, None, (1,))


def test_parse_defaults():
    opts = parse_render_args(["a.mid"])
    assert opts.engine == "fluidsynth"
    assert opts.fmt == "mp3"
    assert opts.soundfont is None
    assert opts.output is None
    assert opts.gain == 0.0
    assert opts.jobs is None
    assert opts.reverb == {} and opts.font_for_channel == {}
    assert opts.reverb_room == 0.75 and opts.reverb_level == 1.0


def test_parse_collects_repeated_channel_options():
    opts = parse_render_args(
        ["a.mid", "--reverb", "1=10", "--reverb", "3=127", "--font-for-channel", "2=Alt",
         "--gain", "-3", "--jobs", "2"]
    )
    assert opts.reverb == {1: 10, 3: 127}
    assert opts.font_for_channel == {2: "Alt"}
    assert opts.gain == -3.0
    assert opts.jobs == 2


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--gain", "1"],
        ["a.mid", "b.mid"],
        ["a.mid", "--bogus"],
        ["a.mid", "--fmt", "ogg"],
        ["a.mid", "--engine", "sf2"],
        ["a.mid", "--gain"],
        ["a.mid", "--gain", "13"],
        ["a.mid", "--gain", "nan"],
        ["a.mid", "--jobs", "0"],
        ["a.mid", "--jobs", "65"],
        ["a.mid", "--reverb-room", "1.5"],
        ["a.mid", "--reverb", "1=128"],
        ["a.mid", "--reverb", "1=10", "--reverb", "1=20"],
        ["a.mid", "--reverb", "17=10"],
        ["a.mid", "--font-for-channel", "17=GM"],
        ["a.mid", "--font-for-channel", "1="],
        ["a.mid", "--font-for-channel", "1=GM", "--font-for-channel", "1=Alt"],
        ["a.mid", "--soundfont", ""],
        ["a.mid", "-o", ""],
        ["a.mid", "--engine", "muse", "--soundfont", "GM"],
        ["a.mid", "--engine", "muse", "--font-for-channel", "1=GM"],
    ],
)
def test_parse_rejects_invalid_arguments(argv):
    with pytest.raises(CgmusicError) as exc:
        parse_render_args(argv)
    assert exc.value.code == BAD_INPUT


def test_source_channels_sorted_and_validated():
    assert source_channels(_part("P", [2, 1])) == (1, 2)
    assert source_channels(_part("P", 10)) == (10,)
    for bad in (0, 17, True, [], [1, "2"]):
        with pytest.raises(CgmusicError) as exc:
            source_channels(_part("P", bad))
        assert exc.value.code == BAD_INPUT


def test_reverb_send_is_written_to_mix_as_normalized_value():
    doc = _doc(_part("P", 2))
    tasks = resolve_parts(doc, _opts(reverb={2: 127}))
    assert doc["parts"][0]["mix"]["reverb_send"] == 1.0
    assert tasks[0].channels == (2,)


def test_font_for_channel_applies_per_source_channel():
    doc = _doc(_part("A", 1), _part("B", 2))
    tasks = {
        t.id: t for t in resolve_parts(doc, _opts(soundfont="GM", font_for_channel={2: "Alt"}))
    }
    assert tasks["A"].soundfont == "GM"
    assert tasks["B"].soundfont == "Alt"


def test_global_soundfont_applies_only_to_fluidsynth_parts():
    doc = _doc(_part("A", 1, hint="muse"), _part("B", 2, hint="fluidsynth"))
    tasks = {t.id: t for t in resolve_parts(doc, _opts(soundfont="GM"))}
    assert tasks["A"].engine == "muse" and tasks["A"].soundfont is None
    assert tasks["B"].engine == "fluidsynth" and tasks["B"].soundfont == "GM"


def test_engine_hint_overrides_engine_option():
    doc = _doc(_part("A", 1, hint="muse"), _part("B", 2))
    tasks = {t.id: t for t in resolve_parts(doc, _opts(engine="fluidsynth"))}
    assert tasks["A"].engine == "muse"
    assert tasks["B"].engine == "fluidsynth"


def test_merged_part_with_conflicting_reverbs_is_rejected():
    doc = _doc(_part("M", [1, 2]))
    with pytest.raises(CgmusicError) as exc:
        resolve_parts(doc, _opts(reverb={1: 10, 2: 20}))
    assert exc.value.code == BAD_INPUT


def test_merged_part_with_conflicting_fonts_is_rejected():
    doc = _doc(_part("M", [1, 2]))
    with pytest.raises(CgmusicError) as exc:
        resolve_parts(doc, _opts(font_for_channel={1: "GM", 2: "Alt"}))
    assert exc.value.code == BAD_INPUT


def test_merged_part_with_identical_reverbs_is_accepted():
    doc = _doc(_part("M", [1, 2]))
    resolve_parts(doc, _opts(reverb={1: 10, 2: 10}))
    assert doc["parts"][0]["mix"]["reverb_send"] == pytest.approx(10 / 127)


def test_font_on_muse_part_is_rejected():
    doc = _doc(_part("A", 1, hint="muse"))
    with pytest.raises(CgmusicError) as exc:
        resolve_parts(doc, _opts(font_for_channel={1: "GM"}))
    assert exc.value.code == BAD_INPUT


def test_worker_count_respects_muse_cap_and_jobs(monkeypatch):
    monkeypatch.setattr(pipeline.os, "cpu_count", lambda: 16)
    fluid = [_task(f"F{i}") for i in range(20)]
    muse = [_task(f"M{i}", "muse") for i in range(6)]
    assert pipeline.worker_count(_opts(), fluid) == 8
    assert pipeline.worker_count(_opts(), muse) == 4
    assert pipeline.worker_count(_opts(), muse[:3]) == 3
    assert pipeline.worker_count(_opts(jobs=2), fluid) == 2
    assert pipeline.worker_count(_opts(), fluid[:3]) == 3


def test_worker_count_follows_cpu_count_when_small(monkeypatch):
    monkeypatch.setattr(pipeline.os, "cpu_count", lambda: 2)
    assert pipeline.worker_count(_opts(), [_task(f"F{i}") for i in range(5)]) == 2


def test_cgsynth_json_error_is_preserved():
    err = from_cgsynth_stderr('noise\n{"error":"muse_unavailable","message":"no muse"}\n', 1)
    assert err.code == "muse_unavailable"
    assert err.message == "no muse"


def test_plain_stderr_becomes_synth_failed():
    err = from_cgsynth_stderr("boom", 2)
    assert err.code == SYNTH_FAILED
