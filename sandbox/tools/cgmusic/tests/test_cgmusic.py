"""cgmusic 端到端编排测试(使用假 cgsynth,cgscore 与 cgmaster 为真实实现)。"""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from helpers import plan_parts, run_cli, write_midi

from cgmusic import pipeline

REPORT_KEYS = {
    "path",
    "fmt",
    "duration_s",
    "lufs",
    "true_peak_dbtp",
    "lra",
    "clipped",
    "balance",
    "reverb",
    "fonts",
    "missing_presets",
    "fallbacks",
    "muse",
    "parts",
    "wall_s",
}


def _ids(mid: Path, scratch: Path) -> list[str]:
    return [part["id"] for part in plan_parts(mid, scratch)]


def test_single_part_render_reports_all_keys(tmp_path, tmp_base, fake_cgsynth, capsys):
    mid = write_midi(tmp_path / "song.mid", channels=(0,))
    code, report, err = run_cli(
        ["render", str(mid), "--fmt", "wav", "-o", str(tmp_path / "out" / "song.wav")], capsys
    )
    assert code == 0, err
    assert set(report) == REPORT_KEYS
    assert report["fmt"] == "wav"
    assert Path(report["path"]).is_file()
    assert [part["engine"] for part in report["parts"]] == ["fluidsynth"]
    [call] = fake_cgsynth.calls()
    assert call["soundfont"] == "GM"
    assert list(tmp_base.iterdir()) == []


def test_muse_uses_dedicated_binary_and_home(
    tmp_path, tmp_base, fake_cgsynth, monkeypatch, capsys
):
    mid = write_midi(tmp_path / "muse-home.mid")
    monkeypatch.setenv("HOME", "/parent-home")
    monkeypatch.setenv("CGMUSIC_MUSE_HOME", "/mounted-muse-home")
    code, _, err = run_cli(
        ["render", str(mid), "--engine", "muse", "--fmt", "wav", "-o", str(tmp_path / "o.wav")],
        capsys,
    )
    assert code == 0, err
    [call] = fake_cgsynth.calls()
    assert call["binary"] == str(fake_cgsynth.muse_binary)
    assert call["home"] == "/mounted-muse-home"


def test_fluidsynth_ignores_muse_binary_and_home(
    tmp_path, tmp_base, fake_cgsynth, monkeypatch, capsys
):
    mid = write_midi(tmp_path / "fluid-home.mid")
    monkeypatch.setenv("HOME", "/parent-home")
    monkeypatch.setenv("CGMUSIC_MUSE_HOME", "/mounted-muse-home")
    code, _, err = run_cli(
        ["render", str(mid), "--fmt", "wav", "-o", str(tmp_path / "o.wav")], capsys
    )
    assert code == 0, err
    [call] = fake_cgsynth.calls()
    assert call["binary"] == str(fake_cgsynth.binary)
    assert call["home"] == "/parent-home"


def test_muse_without_home_override_preserves_parent_home(
    tmp_path, tmp_base, fake_cgsynth, monkeypatch, capsys
):
    mid = write_midi(tmp_path / "muse-default-home.mid")
    monkeypatch.setenv("HOME", "/parent-home")
    monkeypatch.delenv("CGMUSIC_MUSE_HOME", raising=False)
    code, _, err = run_cli(
        ["render", str(mid), "--engine", "muse", "--fmt", "wav", "-o", str(tmp_path / "o.wav")],
        capsys,
    )
    assert code == 0, err
    [call] = fake_cgsynth.calls()
    assert call["binary"] == str(fake_cgsynth.muse_binary)
    assert call["home"] == "/parent-home"


def test_muse_fallback_restores_fluidsynth_binary_and_home(
    tmp_path, tmp_base, fake_cgsynth, monkeypatch, capsys
):
    mid = write_midi(tmp_path / "muse-fallback-home.mid")
    monkeypatch.setenv("HOME", "/parent-home")
    monkeypatch.setenv("CGMUSIC_MUSE_HOME", "/mounted-muse-home")
    monkeypatch.setenv("FAKE_CGSYNTH_FAIL", "ALL")
    monkeypatch.setenv("FAKE_CGSYNTH_FAIL_ENGINE", "muse")
    monkeypatch.setenv("FAKE_CGSYNTH_FAIL_CODE", "muse_unavailable")
    code, _, err = run_cli(
        ["render", str(mid), "--engine", "muse", "--fmt", "wav", "-o", str(tmp_path / "o.wav")],
        capsys,
    )
    assert code == 1 and err["error"] == "muse_unavailable"
    calls = {call["engine"]: call for call in fake_cgsynth.calls()}
    assert calls["muse"]["binary"] == str(fake_cgsynth.muse_binary)
    assert calls["muse"]["home"] == "/mounted-muse-home"
    assert calls["fluidsynth"]["binary"] == str(fake_cgsynth.binary)
    assert calls["fluidsynth"]["home"] == "/parent-home"


def test_parts_render_in_parallel(tmp_path, tmp_base, fake_cgsynth, monkeypatch, capsys):
    mid = write_midi(tmp_path / "multi.mid", channels=(0, 1, 2))
    monkeypatch.setenv("FAKE_CGSYNTH_SLEEP", "1.0")
    code, report, err = run_cli(
        ["render", str(mid), "--fmt", "wav", "--jobs", "3", "-o", str(tmp_path / "out" / "m.wav")],
        capsys,
    )
    assert code == 0, err
    assert len(report["parts"]) == 3
    spans = list(fake_cgsynth.intervals().values())
    assert len(spans) == 3
    assert max(start for start, _ in spans) < min(end for _, end in spans)


def test_jobs_one_serializes_parts(tmp_path, tmp_base, fake_cgsynth, monkeypatch, capsys):
    mid = write_midi(tmp_path / "pair.mid", channels=(0, 1))
    monkeypatch.setenv("FAKE_CGSYNTH_SLEEP", "0.3")
    code, _, err = run_cli(
        ["render", str(mid), "--fmt", "wav", "--jobs", "1", "-o", str(tmp_path / "out" / "p.wav")],
        capsys,
    )
    assert code == 0, err
    spans = sorted(fake_cgsynth.intervals().values())
    assert len(spans) == 2
    assert all(
        prev_end <= next_start
        for (_, prev_end), (next_start, _) in zip(spans, spans[1:], strict=False)
    )


def test_first_failure_cancels_running_parts(tmp_path, tmp_base, fake_cgsynth, monkeypatch, capsys):
    mid = write_midi(tmp_path / "trio.mid", channels=(0, 1, 2))
    failing = _ids(mid, tmp_path / "scratch")[0]
    monkeypatch.setenv("FAKE_CGSYNTH_SLEEP", "30")
    monkeypatch.setenv("FAKE_CGSYNTH_FAIL", failing)
    started = time.monotonic()
    code, report, err = run_cli(
        ["render", str(mid), "--fmt", "wav", "--jobs", "3", "-o", str(tmp_path / "out" / "t.wav")],
        capsys,
    )
    assert time.monotonic() - started < 15
    assert code == 1 and report is None
    assert err == {"error": "synth_failed", "message": f"fake failure for {failing}"}
    assert fake_cgsynth.ends() == []
    assert list(tmp_base.iterdir()) == []


def test_muse_error_code_is_preserved(tmp_path, tmp_base, fake_cgsynth, monkeypatch, capsys):
    mid = write_midi(tmp_path / "m.mid", channels=(0,))
    monkeypatch.setenv("FAKE_CGSYNTH_FAIL", _ids(mid, tmp_path / "scratch")[0])
    monkeypatch.setenv("FAKE_CGSYNTH_FAIL_CODE", "muse_unavailable")
    code, _, err = run_cli(
        ["render", str(mid), "--engine", "muse", "--fmt", "wav", "-o", str(tmp_path / "o.wav")],
        capsys,
    )
    assert code == 1
    assert err["error"] == "muse_unavailable"
    assert list(tmp_base.iterdir()) == []


def test_muse_report_aggregates_parts_and_fallbacks(
    tmp_path, tmp_base, fake_cgsynth, monkeypatch, capsys
):
    mid = write_midi(tmp_path / "duo.mid", channels=(0, 1))
    ids = _ids(mid, tmp_path / "scratch")
    monkeypatch.setenv("FAKE_CGSYNTH_FALLBACK", ids[0])
    code, report, err = run_cli(
        ["render", str(mid), "--engine", "muse", "--fmt", "wav", "-o", str(tmp_path / "o.wav")],
        capsys,
    )
    assert code == 0, err
    assert report["muse"]["version"] == "fake-1"
    assert sorted(track["part"] for track in report["muse"]["tracks"]) == sorted(ids)
    assert report["fallbacks"] == [
        {"from": "muse", "to": "fluidsynth", "reason": "fake", "part": ids[0]}
    ]
    assert report["fonts"] == [] and report["missing_presets"] == []
    assert [part["engine"] for part in report["parts"]] == ["muse", "muse"]


def test_missing_presets_are_attributed_to_their_part(
    tmp_path, tmp_base, fake_cgsynth, monkeypatch, capsys
):
    mid = write_midi(tmp_path / "duo.mid", channels=(0, 1))
    ids = _ids(mid, tmp_path / "scratch")
    monkeypatch.setenv("FAKE_CGSYNTH_MISSING", ids[1])
    code, report, err = run_cli(
        ["render", str(mid), "--fmt", "wav", "-o", str(tmp_path / "o.wav")], capsys
    )
    assert code == 0, err
    assert [m["part"] for m in report["missing_presets"]] == [ids[1]]
    [font] = report["fonts"]
    assert font["id"] == "GM"
    assert sorted(font["parts"]) == sorted(ids)
    assert [m["part"] for m in font["missing_presets"]] == [ids[1]]


def test_font_for_channel_maps_by_source_channel(tmp_path, tmp_base, fake_cgsynth, capsys):
    mid = write_midi(tmp_path / "duo.mid", channels=(0, 1))
    parts = plan_parts(mid, tmp_path / "scratch")
    channel2 = next(part["id"] for part in parts if part["source_channel"] == 2)
    code, _, err = run_cli(
        [
            "render",
            str(mid),
            "--font-for-channel",
            "2=Alt",
            "--fmt",
            "wav",
            "-o",
            str(tmp_path / "o.wav"),
        ],
        capsys,
    )
    assert code == 0, err
    fonts = {call["part"]: call["soundfont"] for call in fake_cgsynth.calls()}
    assert fonts[channel2] == "Alt"
    assert [font for part, font in fonts.items() if part != channel2] == ["GM"]


def test_duration_limit_rejects_before_synthesis(
    tmp_path, tmp_base, fake_cgsynth, monkeypatch, capsys
):
    monkeypatch.setattr(pipeline, "MAX_DURATION_S", 1.0)
    mid = write_midi(tmp_path / "long.mid")
    code, _, err = run_cli(
        ["render", str(mid), "--fmt", "wav", "-o", str(tmp_path / "o.wav")], capsys
    )
    assert code == 1 and err["error"] == "too_long"
    assert fake_cgsynth.calls() == []
    assert list(tmp_base.iterdir()) == []


@pytest.mark.parametrize("limit", ["MAX_MIDI_BYTES", "MAX_INPUT_BYTES"])
def test_size_limits_reject_as_too_long(
    tmp_path, tmp_base, fake_cgsynth, monkeypatch, capsys, limit
):
    monkeypatch.setattr(pipeline, limit, 10)
    mid = write_midi(tmp_path / "big.mid")
    code, _, err = run_cli(
        ["render", str(mid), "--fmt", "wav", "-o", str(tmp_path / "o.wav")], capsys
    )
    assert code == 1 and err["error"] == "too_long"
    assert fake_cgsynth.calls() == []


def test_unsupported_suffix_is_rejected(tmp_path, capsys):
    text = tmp_path / "notes.txt"
    text.write_text("not music", encoding="utf-8")
    code, _, err = run_cli(["render", str(text)], capsys)
    assert code == 1 and err["error"] == "unsupported"


def test_missing_input_is_bad_input(tmp_path, capsys):
    code, _, err = run_cli(["render", str(tmp_path / "absent.mid")], capsys)
    assert code == 1 and err["error"] == "bad_input"


def test_missing_binary_is_synth_failed(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CGSYNTH_BIN", str(tmp_path / "nonexistent" / "cgsynth"))
    mid = write_midi(tmp_path / "m.mid")
    code, _, err = run_cli(["render", str(mid), "-o", str(tmp_path / "o.mp3")], capsys)
    assert code == 1 and err["error"] == "synth_failed"


def test_presets_default_uses_default_font(fake_cgsynth, capsys):
    code, out, err = run_cli(["presets"], capsys)
    assert code == 0, err
    assert out["soundfont"] == "GM"


def test_presets_explicit_font(fake_cgsynth, capsys):
    code, out, err = run_cli(["presets", "--soundfont", "Alt"], capsys)
    assert code == 0, err
    assert out["soundfont"] == "Alt"


def test_soundfonts_passthrough(fake_cgsynth, capsys):
    code, out, err = run_cli(["soundfonts"], capsys)
    assert code == 0, err
    assert [font["id"] for font in out["fonts"]] == ["GM", "Alt"]


def test_analyze_reports_loudness_and_duration(tmp_path, capsys):
    rate = 44100
    tone = 0.5 * np.sin(2 * np.pi * 440 * np.arange(4 * rate) / rate)
    path = tmp_path / "tone.wav"
    sf.write(str(path), np.stack([tone, tone], axis=1).astype(np.float32), rate, subtype="FLOAT")
    code, out, err = run_cli(["analyze", str(path)], capsys)
    assert code == 0, err
    assert set(out) == {"lufs", "true_peak_dbtp", "lra", "clipped", "duration_s"}
    assert out["duration_s"] == pytest.approx(4.0, abs=0.01)


def test_help_is_unsupported(capsys):
    code, _, err = run_cli(["--help"], capsys)
    assert code == 1 and err["error"] == "unsupported"


def test_no_arguments_is_bad_input(capsys):
    code, _, err = run_cli([], capsys)
    assert code == 1 and err["error"] == "bad_input"


def test_muse_partial_fallback_rerenders_with_fluidsynth(
    tmp_path, tmp_base, fake_cgsynth, monkeypatch, capsys
):
    mid = write_midi(tmp_path / "duo.mid", channels=(0, 1))
    ids = _ids(mid, tmp_path / "scratch")
    monkeypatch.setenv("FAKE_CGSYNTH_FAIL", ids[0])
    monkeypatch.setenv("FAKE_CGSYNTH_FAIL_ENGINE", "muse")
    monkeypatch.setenv("FAKE_CGSYNTH_FAIL_CODE", "muse_unavailable")
    code, report, err = run_cli(
        ["render", str(mid), "--engine", "muse", "--fmt", "wav", "-o", str(tmp_path / "o.wav")],
        capsys,
    )
    assert code == 0, err
    assert Path(report["path"]).is_file()
    fallback_parts = [fb["part"] for fb in report["fallbacks"]]
    assert ids[0] in fallback_parts
    fb_entry = next(fb for fb in report["fallbacks"] if fb["part"] == ids[0])
    assert fb_entry["reason"] == "muse_unavailable"
    assert fb_entry["channel"] == 1
    part_engines = {p["id"]: p["engine"] for p in report["parts"]}
    assert part_engines[ids[0]] == "fluidsynth"
    assert part_engines[ids[1]] == "muse"
    assert report["muse"] is not None
    assert [t["part"] for t in report["muse"]["tracks"]] == [ids[1]]
    assert len(report["fonts"]) == 1
    assert report["fonts"][0]["parts"] == [ids[0]]


def test_muse_silent_fallback_records_muse_silent_reason(
    tmp_path, tmp_base, fake_cgsynth, monkeypatch, capsys
):
    mid = write_midi(tmp_path / "duo.mid", channels=(0, 1))
    ids = _ids(mid, tmp_path / "scratch")
    monkeypatch.setenv("FAKE_CGSYNTH_FAIL", ids[0])
    monkeypatch.setenv("FAKE_CGSYNTH_FAIL_ENGINE", "muse")
    monkeypatch.setenv("FAKE_CGSYNTH_FAIL_CODE", "muse_unavailable")
    monkeypatch.setenv("FAKE_CGSYNTH_FAIL_MSG", "MuseSampler 输出全静音")
    code, report, err = run_cli(
        ["render", str(mid), "--engine", "muse", "--fmt", "wav", "-o", str(tmp_path / "o.wav")],
        capsys,
    )
    assert code == 0, err
    fb_entry = next(fb for fb in report["fallbacks"] if fb["part"] == ids[0])
    assert fb_entry["reason"] == "muse_silent"


def test_muse_all_parts_fallback_fails_with_muse_unavailable(
    tmp_path, tmp_base, fake_cgsynth, monkeypatch, capsys
):
    mid = write_midi(tmp_path / "duo.mid", channels=(0, 1))
    monkeypatch.setenv("FAKE_CGSYNTH_FAIL", "ALL")
    monkeypatch.setenv("FAKE_CGSYNTH_FAIL_ENGINE", "muse")
    monkeypatch.setenv("FAKE_CGSYNTH_FAIL_CODE", "muse_unavailable")
    code, report, err = run_cli(
        ["render", str(mid), "--engine", "muse", "--fmt", "wav", "-o", str(tmp_path / "o.wav")],
        capsys,
    )
    assert code == 1 and report is None
    assert err["error"] == "muse_unavailable"


def test_non_muse_error_does_not_fallback(tmp_path, tmp_base, fake_cgsynth, monkeypatch, capsys):
    mid = write_midi(tmp_path / "duo.mid", channels=(0, 1))
    ids = _ids(mid, tmp_path / "scratch")
    monkeypatch.setenv("FAKE_CGSYNTH_FAIL", ids[0])
    monkeypatch.setenv("FAKE_CGSYNTH_FAIL_ENGINE", "muse")
    monkeypatch.setenv("FAKE_CGSYNTH_FAIL_CODE", "synth_failed")
    code, report, err = run_cli(
        ["render", str(mid), "--engine", "muse", "--fmt", "wav", "-o", str(tmp_path / "o.wav")],
        capsys,
    )
    assert code == 1 and report is None
    assert err["error"] == "synth_failed"
    fluid_calls = [c for c in fake_cgsynth.calls() if c["engine"] == "fluidsynth"]
    assert fluid_calls == []
