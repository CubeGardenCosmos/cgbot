"""真实 MIDI 样本的非门控检查(默认不运行,使用 `pytest -m realfiles`)。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cgmidi.cli import main

pytestmark = pytest.mark.realfiles

SAMPLES = sorted(Path("/tmp/cgtest").glob("*.mid"))


@pytest.mark.parametrize("path", SAMPLES, ids=lambda p: p.name)
def test_speed_change_keeps_note_count(path, tmp_path, capsys):
    assert main(["info", str(path)]) == 0
    info = json.loads(capsys.readouterr().out)

    out = tmp_path / "out.mid"
    assert main(["process", str(path), "-o", str(out), "--speed", "1.25"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["notes"] == info["notes"]


SNOWY_FILE = Path("/mnt/g/Users/klp/Downloads") / (
    "HOYO-MiX _ Arr. Flareonite - Snowy Reunion_Snezhnaya Overworld Theme (Day).mid"
)


@pytest.mark.skipif(not SNOWY_FILE.exists(), reason="Snowy test file not available")
def test_snowy_reunion_hands_bars_and_pedal(tmp_path, capsys):
    assert main(["bars", str(SNOWY_FILE)]) == 0
    bars_data = json.loads(capsys.readouterr().out)
    assert bars_data["total_bars"] == 47
    assert bars_data["total_notes"] == 505
    assert sum(len(b["harmony"]) for b in bars_data["bars"]) > 0

    out_hands = tmp_path / "hands.mid"
    assert main(["hands", str(SNOWY_FILE), "-o", str(out_hands)]) == 0
    hands_data = json.loads(capsys.readouterr().out)
    assert hands_data["channels_merged"] == [1]
    assert hands_data["notes_changed"] == 181
    assert hands_data["accents_kept"] == 7

    out_pedal = tmp_path / "pedal.mid"
    assert main(["pedal", str(SNOWY_FILE), "-o", str(out_pedal), "--mode", "auto"]) == 0
    pedal_data = json.loads(capsys.readouterr().out)
    assert pedal_data["status"] == "already_pedaled"
    assert pedal_data["existing_pedal_events"] == 124
    assert not out_pedal.exists()

    assert (
        main(
            [
                "pedal",
                str(SNOWY_FILE),
                "-o",
                str(out_pedal),
                "--mode",
                "auto",
                "--replace",
            ]
        )
        == 0
    )
    replace_data = json.loads(capsys.readouterr().out)
    assert replace_data["status"] == "ok"
    assert replace_data["removed_pedal_events"] == 124
    assert replace_data["pedal_events"] > 0
    assert out_pedal.exists()
