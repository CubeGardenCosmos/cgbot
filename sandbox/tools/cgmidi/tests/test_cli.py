"""cgmidi 命令行的离线测试(通过 main 调用,检查 stdout/stderr 与退出码)。"""

from __future__ import annotations

import json

import pytest
from midi_helpers import absolute, note_off, note_on, notes_of, tempo, write_midi
from mido import MidiFile

from cgmidi.cli import main


def _run(argv, capsys):
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _source(tmp_path, note=60):
    return write_midi(
        tmp_path / "song.mid",
        [[tempo(0, 500_000), note_on(0, 0, note), note_off(480, 0, note)]],
    )


def test_info_prints_summary(tmp_path, capsys):
    code, out, _ = _run(["info", str(_source(tmp_path))], capsys)
    assert code == 0
    data = json.loads(out)
    assert data["ticks_per_beat"] == 480
    assert data["notes"] == 1
    assert data["channels"] == [
        {"channel": 1, "notes": 1, "note_range": [60, 60], "programs": [], "banks": []}
    ]


def test_process_applies_options_and_writes_file(tmp_path, capsys):
    src = _source(tmp_path)
    out_path = tmp_path / "out.mid"
    code, out, _ = _run(
        [
            "process",
            str(src),
            "-o",
            str(out_path),
            "--transpose",
            "2",
            "--speed",
            "2",
            "--set-bank",
            "1=3:4",
            "--map-channel",
            "1:2",
        ],
        capsys,
    )
    assert code == 0
    summary = json.loads(out)
    assert summary["output"] == str(out_path)
    assert summary["notes"] == 1

    events = absolute(MidiFile(str(out_path)).tracks[0])
    assert notes_of(events) == [(0, "note_on", 1, 62), (480, "note_off", 1, 62)]
    assert [m.tempo for _, m in events if m.type == "set_tempo"] == [250_000]
    assert [
        (m.control, m.value, m.channel)
        for _, m in events
        if m.type == "control_change" and m.control in (0, 32)
    ] == [
        (0, 3, 1),
        (32, 4, 1),
    ]


def test_missing_input_is_bad_input(tmp_path, capsys):
    code, _, err = _run(["info", str(tmp_path / "missing.mid")], capsys)
    assert code == 1
    assert json.loads(err)["error"] == "bad_input"


def test_garbage_bytes_are_parse_failed(tmp_path, capsys):
    bad = tmp_path / "bad.mid"
    bad.write_bytes(b"not a midi file at all")
    code, _, err = _run(["info", str(bad)], capsys)
    assert code == 1
    assert json.loads(err)["error"] == "parse_failed"


def test_format_two_is_unsupported_and_writes_nothing(tmp_path, capsys):
    src = write_midi(tmp_path / "fmt2.mid", [[note_on(0, 0, 60), note_off(480, 0, 60)]], mtype=2)
    out_path = tmp_path / "out.mid"
    code, _, err = _run(["process", str(src), "-o", str(out_path)], capsys)
    assert code == 1
    assert json.loads(err)["error"] == "unsupported_format"
    assert not out_path.exists()


def test_transpose_out_of_range_exit_code(tmp_path, capsys):
    src = _source(tmp_path, note=120)
    out_path = tmp_path / "out.mid"
    code, _, err = _run(["process", str(src), "-o", str(out_path), "--transpose", "12"], capsys)
    assert code == 1
    assert json.loads(err)["error"] == "out_of_range"
    assert not out_path.exists()


@pytest.mark.parametrize(
    "extra",
    [
        ["--mute", "0"],
        ["--map-channel", "1"],
        ["--set-program", "1=200"],
        ["--set-bank", "x=1"],
    ],
)
def test_bad_arguments_are_bad_input(tmp_path, capsys, extra):
    src = _source(tmp_path)
    code, _, err = _run(["process", str(src), "-o", str(tmp_path / "out.mid"), *extra], capsys)
    assert code == 1
    assert json.loads(err)["error"] == "bad_input"


def test_same_input_and_output_is_bad_input(tmp_path, capsys):
    src = _source(tmp_path)
    code, _, err = _run(["process", str(src), "-o", str(src)], capsys)
    assert code == 1
    assert json.loads(err)["error"] == "bad_input"


def test_missing_output_flag_exits_two_with_json(tmp_path, capsys):
    src = _source(tmp_path)
    with pytest.raises(SystemExit) as exc:
        main(["process", str(src)])
    assert exc.value.code == 2
    assert json.loads(capsys.readouterr().err)["error"] == "bad_input"


def test_bars_cli_outputs_json(tmp_path, capsys):
    src = _source(tmp_path)
    code, out, _ = _run(["bars", str(src)], capsys)
    assert code == 0
    data = json.loads(out)
    assert data["total_bars"] == 1
    assert data["bars"][0]["time_signature"] == "4/4"


def test_hands_cli_merges_and_writes(tmp_path, capsys):
    src = write_midi(
        tmp_path / "hands.mid",
        [
            [note_on(0, 0, 72, 40), note_off(480, 0, 72)],
            [note_on(0, 0, 48, 60), note_off(480, 0, 48)],
        ],
    )
    out_path = tmp_path / "hands_out.mid"
    code, out, _ = _run(["hands", str(src), "-o", str(out_path)], capsys)
    assert code == 0
    report = json.loads(out)
    assert report["notes_changed"] == 1
    assert out_path.exists()


def test_pedal_cli_honors_existing_and_replaces(tmp_path, capsys):
    src = write_midi(
        tmp_path / "pedal.mid",
        [[note_on(0, 0, 60), note_off(480, 0, 60)]],
    )
    out_path = tmp_path / "pedal_out.mid"
    code, out, _ = _run(["pedal", str(src), "-o", str(out_path), "--mode", "bar"], capsys)
    assert code == 0
    first = json.loads(out)
    assert first["status"] == "ok"
    assert out_path.exists()

    code, out, _ = _run(["pedal", str(out_path), "-o", str(tmp_path / "noop.mid")], capsys)
    assert code == 0
    second = json.loads(out)
    assert second["status"] == "already_pedaled"
    assert not (tmp_path / "noop.mid").exists()
