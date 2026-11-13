"""测试辅助:生成 MIDI、通过 cgscore 查询部件表、捕获 CLI 输出。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import mido
from cgscore.prepare import prepare

from cgmusic.cli import main


def write_midi(path: Path, channels: tuple[int, ...] = (0,), notes: int = 8) -> Path:
    """每个通道一条音轨,每音符 1 拍(480 tpq, 120 BPM),每通道时长约 notes 拍 / 2。"""
    mid = mido.MidiFile(type=1, ticks_per_beat=480)
    meta = mido.MidiTrack()
    meta.append(mido.MetaMessage("set_tempo", tempo=500000, time=0))
    meta.append(mido.MetaMessage("end_of_track", time=0))
    mid.tracks.append(meta)
    for channel in channels:
        track = mido.MidiTrack()
        track.append(mido.Message("program_change", program=0, channel=channel, time=0))
        for index in range(notes):
            note = 60 + index % 12
            track.append(mido.Message("note_on", note=note, velocity=80, channel=channel, time=0))
            track.append(mido.Message("note_off", note=note, velocity=0, channel=channel, time=480))
        track.append(mido.MetaMessage("end_of_track", time=0))
        mid.tracks.append(track)
    mid.save(path)
    return path


def plan_parts(mid: Path, scratch: Path) -> list[dict[str, Any]]:
    """用 cgscore 直接前处理,读取 plan.json 中的部件(id、source_channel 等)。"""
    scratch.mkdir(parents=True, exist_ok=True)
    plan = prepare(mid, scratch)
    return json.loads(Path(plan).read_text(encoding="utf-8"))["parts"]


def run_cli(
    argv: list[str], capsys: Any
) -> tuple[int, dict[str, Any] | None, dict[str, Any] | None]:
    code = main(argv)
    out, err = capsys.readouterr()
    return code, _load(out), _load(err)


def _load(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    return json.loads(stripped) if stripped else None
