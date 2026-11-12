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
