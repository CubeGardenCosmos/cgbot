"""SMF 拆分为每声部一个文件。

规则(与 C++ midi.cpp/render.cpp 的每通道行为一致):
- 通道 9 = 鼓组,所有鼓轨合并为一个声部。
- 其余通道按 (通道, 程序) 分组;同通道同程序的多条轨(如钢琴双手)合并为一个声部。
- 通道内程序随时间变化时,核心按事件渲染,不拆声部;这里同一通道的所有程序段
  合并为一个声部,保留原程序变更序列,plan 的 gm_program 取首程序。
- 每个声部文件保留完整速度表/拍号与全部通道事件(CC/弯音等),通道重映射为 0(鼓 9)。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field

import mido

from .musicxml import DRUM_CHANNEL, guess_program


@dataclass
class _Group:
    channel: int
    program: int
    name: str = ""
    is_drum: bool = False
    messages: list = field(default_factory=list)  # [(abs_tick, seq, msg)]
    initial_program: int = 0
    source_channels: set[int] = field(default_factory=set)

    @property
    def source_channel(self) -> int | list[int]:
        channels = self.source_channels or {(DRUM_CHANNEL if self.is_drum else self.channel) + 1}
        if len(channels) == 1:
            return next(iter(channels))
        return sorted(channels)


def _track_name(track: mido.MidiTrack) -> str:
    for msg in track:
        if msg.type == "track_name" and msg.name:
            return msg.name
    return ""


def _first_program(track: mido.MidiTrack, channel: int) -> int | None:
    for msg in track:
        if msg.type == "program_change" and msg.channel == channel:
            return msg.program
    return None


def split_midi(mid: mido.MidiFile) -> list[_Group]:
    groups: dict[tuple[int, int], _Group] = {}
    conductor: list[tuple[int, int, mido.MetaMessage]] = []
    seq = 0
    for ti, track in enumerate(mid.tracks):
        abs_tick = 0
        name = _track_name(track)
        for msg in track:
            abs_tick += msg.time
            if msg.is_meta:
                if msg.type in ("set_tempo", "time_signature"):
                    conductor.append((abs_tick, seq, copy.copy(msg)))
                    seq += 1
                continue
            channel = getattr(msg, "channel", 0)
            is_drum = channel == DRUM_CHANNEL
            if is_drum:
                key = (DRUM_CHANNEL, -1)
            else:
                program = _first_program(track, channel)
                if program is None:
                    program = guess_program(name.lower()) if name else 0
                key = (channel, program)
            group = groups.get(key)
            if group is None:
                group = _Group(
                    channel=channel,
                    program=key[1],
                    is_drum=is_drum,
                    initial_program=0 if is_drum else key[1],
                )
                groups[key] = group
            if name and not group.name:
                group.name = name
            copied = copy.copy(msg)
            copied.time = 0
            group.messages.append((abs_tick, seq, copied))
            group.source_channels.add(channel + 1)
            seq += 1
        _ = ti

    out: list[_Group] = []
    for group in groups.values():
        # 排序:同刻 note-off 先于 note-on(与 C++ 一致),其余保持原顺序
        def prio(item):
            _tick, s, msg = item
            if msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
                return (0, s)
            return (1, s)

        group.messages.sort(key=lambda item: (item[0], prio(item)))
        group.conductor = sorted(conductor, key=lambda item: item[0])  # type: ignore[attr-defined]
        out.append(group)
    # 稳定顺序:按首次出现的通道/程序排序(鼓最后)
    out.sort(key=lambda g: (g.is_drum, g.channel, g.program))
    return out


def group_to_midi(group: _Group, channel: int, ppq: int) -> mido.MidiFile:
    mid = mido.MidiFile(type=1, ticks_per_beat=ppq)
    track = mido.MidiTrack()
    mid.tracks.append(track)
    track.append(mido.MetaMessage("track_name", name=group.name or "", time=0))
    events: list[tuple[int, int, int, mido.Message]] = []
    seq = 0

    def add(tick: int, prio: int, msg) -> None:
        nonlocal seq
        events.append((tick, prio, seq, msg))
        seq += 1

    for tick, _s, msg in group.conductor:  # type: ignore[attr-defined]
        add(tick, 0, copy.copy(msg))
    if not group.is_drum:
        add(
            0,
            1,
            mido.Message("program_change", channel=channel, program=group.initial_program, time=0),
        )
    for tick, s, msg in group.messages:
        copied = copy.copy(msg)
        if hasattr(copied, "channel"):
            copied.channel = channel
        # 原顺序已在 split_midi 排好;prio 2 保持相对次序
        add(tick, 2 + (s % 2) * 0, copied)
    events.sort(key=lambda e: (e[0], e[1], e[2]))
    previous = 0
    for tick, _p, _s, msg in events:
        msg.time = tick - previous
        previous = tick
        track.append(msg)
    track.append(mido.MetaMessage("end_of_track", time=0))
    return mid
