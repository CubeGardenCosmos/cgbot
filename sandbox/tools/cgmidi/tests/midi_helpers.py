"""测试用 MIDI 构造工具:以绝对 tick 描述事件,写入时换算为相对 delta。"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from mido import Message, MetaMessage, MidiFile, MidiTrack

Event = tuple[int, Any]  # (绝对 tick, mido 消息)


def note_on(tick: int, ch: int, note: int, vel: int = 100) -> Event:
    return tick, Message("note_on", channel=ch, note=note, velocity=vel)


def note_off(tick: int, ch: int, note: int) -> Event:
    return tick, Message("note_off", channel=ch, note=note, velocity=0)


def tempo(tick: int, value: int) -> Event:
    return tick, MetaMessage("set_tempo", tempo=value)


def program(tick: int, ch: int, prog: int) -> Event:
    return tick, Message("program_change", channel=ch, program=prog)


def control(tick: int, ch: int, cc: int, value: int) -> Event:
    return tick, Message("control_change", channel=ch, control=cc, value=value)


def track_from(events: Iterable[Event]) -> MidiTrack:
    """按 tick 稳定排序后转为相对 delta 的 MidiTrack。"""
    track = MidiTrack()
    prev = 0
    for tick, msg in sorted(events, key=lambda event: event[0]):
        track.append(msg.copy(time=tick - prev))
        prev = tick
    return track


def build_midi(tracks: list[list[Event]], *, tpb: int = 480, mtype: int = 1) -> MidiFile:
    mid = MidiFile(type=mtype, ticks_per_beat=tpb)
    mid.tracks.extend(track_from(events) for events in tracks)
    return mid


def write_midi(
    path: Path, tracks: list[list[Event]], *, tpb: int = 480, mtype: int = 1
) -> Path:
    build_midi(tracks, tpb=tpb, mtype=mtype).save(str(path))
    return path


def absolute(track: Iterable[Any]) -> list[Event]:
    tick = 0
    out: list[Event] = []
    for msg in track:
        tick += msg.time
        out.append((tick, msg))
    return out


def notes_of(events: list[Event]) -> list[tuple[int, str, int, int]]:
    """仅保留音符事件: (tick, 类型, 通道, 音高)。"""
    return [
        (tick, msg.type, msg.channel, msg.note)
        for tick, msg in events
        if msg.type in ("note_on", "note_off")
    ]
