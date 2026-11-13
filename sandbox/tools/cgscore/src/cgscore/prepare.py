"""cgscore prepare:输入乐谱 -> parts/<id>.mid + plan.json。"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import mido

from .defaults import default_mix, family_for
from .midisplit import group_to_midi, split_midi
from .model import Part, write_plan
from .musicxml import DRUM_CHANNEL, parse_score, part_to_midi

_SUFFIXES = {".mid", ".midi", ".musicxml", ".xml", ".mxl"}


def _used_ids(parts: list[Part]) -> None:
    seen: set[str] = set()
    for part in parts:
        pid = part.id or "P"
        if pid in seen:
            raise ValueError(f"duplicate part id: {pid}")
        seen.add(pid)


def prepare_midi(path: Path) -> list[Part]:
    mid = mido.MidiFile(path)
    groups = split_midi(mid)
    parts: list[Part] = []
    for index, group in enumerate(groups, start=1):
        channel = DRUM_CHANNEL if group.is_drum else 0
        program = 0 if group.is_drum else group.initial_program
        name = group.name or f"Part {index}"
        muse_name, mix = default_mix(name, program, group.is_drum)
        part = Part(
            id=f"P{index}",
            name=name,
            gm_program=program,
            is_drum=group.is_drum,
            channel=channel,
            muse_name=muse_name,
            family=family_for(program, group.is_drum),
            mid=group_to_midi(group, channel, mid.ticks_per_beat),
            mix=mix,
        )
        part.duration_s = part.mid.length
        parts.append(part)
    return parts


def prepare_musicxml(path: Path) -> list[Part]:
    data = path.read_bytes()
    parsed, tempos, sigs = parse_score(data, path.name)
    # 声部名出现次序:mapping.cpp 的 occurrence(用于 Flute 2 / Violins 2)
    occurrences: Counter[str] = Counter()
    parts: list[Part] = []
    for index, out in enumerate(parsed, start=1):
        channel = DRUM_CHANNEL if out.drum else 0
        lowered = out.name.lower()
        occurrence = occurrences[lowered]
        occurrences[lowered] += 1
        from .defaults import muse_name_for

        muse_name = muse_name_for(out.name, out.program, out.drum, occurrence)
        _muse_default, mix = default_mix(out.name, out.program, out.drum)
        mid = part_to_midi(out, channel, tempos, sigs)
        part = Part(
            id=f"P{index}",
            name=out.name,
            gm_program=out.program,
            is_drum=out.drum,
            channel=channel,
            muse_name=muse_name,
            family=family_for(out.program, out.drum),
            mid=mid,
            mix=mix,
        )
        part.duration_s = mid.length
        parts.append(part)
    return parts


def prepare(input_path: Path, outdir: Path) -> Path:
    suffix = input_path.suffix.lower()
    if suffix not in _SUFFIXES:
        raise ValueError(f"unsupported input type: {suffix} (expected {sorted(_SUFFIXES)})")
    if suffix in (".mid", ".midi"):
        parts = prepare_midi(input_path)
    else:
        parts = prepare_musicxml(input_path)
    if not parts:
        raise ValueError("score has no parts")
    _used_ids(parts)
    parts_dir = outdir / "parts"
    parts_dir.mkdir(parents=True, exist_ok=True)
    for part in parts:
        part.mid.save(parts_dir / f"{part.id}.mid")
    return write_plan(parts, input_path.name, outdir)
