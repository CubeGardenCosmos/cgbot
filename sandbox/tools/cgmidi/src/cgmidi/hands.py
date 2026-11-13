"""合并同通道、同音色的多轨力度基线(例如钢琴左右手)。

只改 note_on 的力度,不改音符、时间、控制器或其他事件。算法使用 tick 域的
滑动中值,因此不依赖速度标记;合成器仍按原速度表播放。
"""

from __future__ import annotations

import bisect
import statistics
from dataclasses import dataclass
from typing import Any

from cgmidi.errors import BAD_INPUT, CgmidiError
from cgmidi.ops import DRUM_CHANNEL, Event, _absolute, _rebuild

DEFAULT_ACCENT_GAP = 12


@dataclass
class _Onset:
    tick: int
    velocity: int
    event_index: int


def _programs(events: list[Event]) -> tuple[int, ...]:
    return tuple(msg.program for _tick, msg in events if msg.type == "program_change")


def _group_tracks(
    tracks: list[list[Event]], channels: set[int] | None
) -> list[tuple[int, tuple[int, ...], list[int]]]:
    # MuseScore 常只在第一轨写 Program Change,同通道其他轨继承该音色。
    channel_programs: dict[int, set[int]] = {}
    for events in tracks:
        for _tick, msg in events:
            if msg.type == "program_change":
                channel_programs.setdefault(msg.channel, set()).add(msg.program)
    groups: dict[tuple[int, tuple[int, ...]], list[int]] = {}
    for index, events in enumerate(tracks):
        onsets = [
            msg
            for _tick, msg in events
            if msg.type == "note_on" and msg.velocity > 0 and msg.channel != DRUM_CHANNEL
        ]
        if not onsets:
            continue
        track_channels = {msg.channel for msg in onsets}
        if len(track_channels) != 1:
            continue
        channel = next(iter(track_channels))
        if channels is not None and channel not in channels:
            continue
        programs = channel_programs.get(channel, {0})
        # 通道内多次换音色时无法把整轨归为同一个乐器,保守跳过。
        if len(programs) != 1:
            continue
        key = (channel, tuple(sorted(programs)))
        groups.setdefault(key, []).append(index)
    return [
        (channel, programs, indices)
        for (channel, programs), indices in groups.items()
        if len(indices) >= 2
    ]


def _baselines(
    onsets: list[_Onset], window_ticks: int
) -> tuple[list[int], list[tuple[int, float]]]:
    """返回每个 onset 的滑动中值,以及用于采样的 (tick, baseline) 序列。

    窗口以当前 onset 为中心,边界收缩;至少取 3 个 onset(文件过短时全取),
    避免窗首尾只剩下重音时把重音误当作基线。
    """
    if not onsets:
        return [], []
    ticks = [onset.tick for onset in onsets]
    velocities = [onset.velocity for onset in onsets]
    values: list[int] = []
    samples: list[tuple[int, float]] = []
    for index, onset in enumerate(onsets):
        left = bisect.bisect_left(ticks, onset.tick - window_ticks)
        right = bisect.bisect_right(ticks, onset.tick + window_ticks)
        if right - left < 3:
            left = max(0, min(index - 1, len(onsets) - 3))
            right = min(len(onsets), left + 3)
        value = float(statistics.median(velocities[left:right]))
        values.append(round(value))
        if samples and samples[-1][0] == onset.tick:
            samples[-1] = (onset.tick, value)
        else:
            samples.append((onset.tick, value))
    return values, samples


def _sample_at(samples: list[tuple[int, float]], tick: int) -> float:
    """阶梯插值:基线由最近一次 onset 决定,避免在两音之间制造虚假渐强。"""
    ticks = [item[0] for item in samples]
    index = bisect.bisect_right(ticks, tick) - 1
    return samples[max(0, index)][1]


def merge_hands(
    tracks: list[list[Event]],
    *,
    channels: set[int] | None = None,
    window_ticks: int = 960,
    accent_gap: int = DEFAULT_ACCENT_GAP,
) -> tuple[list[list[Event]], dict[str, Any]]:
    """合并绝对 tick 轨道的力度基线,返回新轨道与逐项报告。

    每轨先在 `window_ticks` 半径内计算滑动中值;声部基线取各轨在同一
    onset 上的最大值。与自身基线相差小于 `accent_gap` 的音符移到声部
    基线,其余音符保留自己的力度偏移(重音仍位于共同基线之上)。
    """
    if window_ticks <= 0:
        raise CgmidiError(BAD_INPUT, "力度窗口须为正整数 tick")
    if not 0 <= accent_gap <= 127:
        raise CgmidiError(BAD_INPUT, "重音阈值须在 0-127 之间")

    out = [[(tick, msg.copy()) for tick, msg in events] for events in tracks]
    groups = _group_tracks(tracks, channels)
    report: dict[str, Any] = {
        "channels_merged": [],
        "notes_changed": 0,
        "accents_kept": 0,
        "window_ticks": window_ticks,
        "accent_gap": accent_gap,
        "groups": [],
    }
    for channel, programs, indices in groups:
        onset_tracks: list[list[_Onset]] = []
        for track_index in indices:
            onsets = [
                _Onset(tick, msg.velocity, event_index)
                for event_index, (tick, msg) in enumerate(tracks[track_index])
                if msg.type == "note_on" and msg.velocity > 0 and msg.channel == channel
            ]
            onset_tracks.append(onsets)
        baselines = [_baselines(onsets, window_ticks)[0] for onsets in onset_tracks]
        samples = [_baselines(onsets, window_ticks)[1] for onsets in onset_tracks]
        changed = 0
        accents = 0
        before_sum = 0
        after_sum = 0
        for track_index, track_onsets, track_baselines in zip(
            indices, onset_tracks, baselines, strict=True
        ):
            for onset, own_baseline in zip(track_onsets, track_baselines, strict=True):
                part_baseline = round(
                    max(_sample_at(track_samples, onset.tick) for track_samples in samples)
                )
                offset = onset.velocity - own_baseline
                if abs(offset) < accent_gap:
                    new_velocity = part_baseline
                else:
                    new_velocity = part_baseline + offset
                    accents += 1
                new_velocity = max(1, min(127, new_velocity))
                before_sum += onset.velocity
                after_sum += new_velocity
                if new_velocity != onset.velocity:
                    changed += 1
                    tick, msg = out[track_index][onset.event_index]
                    out[track_index][onset.event_index] = (tick, msg.copy(velocity=new_velocity))
        report["channels_merged"].append(channel + 1)
        report["notes_changed"] += changed
        report["accents_kept"] += accents
        report["groups"].append(
            {
                "channel": channel + 1,
                "tracks": indices,
                "programs": list(programs),
                "notes": sum(map(len, onset_tracks)),
                "notes_changed": changed,
                "accents_kept": accents,
                "mean_velocity_before": round(before_sum / sum(map(len, onset_tracks)), 3),
                "mean_velocity_after": round(after_sum / sum(map(len, onset_tracks)), 3),
            }
        )
    report["channels_merged"].sort()
    return out, report


def merge_hands_midi(
    mid: Any,
    *,
    channels: set[int] | None = None,
    window_beats: float = 2.0,
    accent_gap: int = DEFAULT_ACCENT_GAP,
) -> tuple[Any, dict[str, Any]]:
    """MidiFile 接口;默认窗口为前后各两拍。"""
    if window_beats <= 0:
        raise CgmidiError(BAD_INPUT, "--window-beats 须大于 0")
    tracks = [_absolute(track) for track in mid.tracks]
    merged, report = merge_hands(
        tracks,
        channels=channels,
        window_ticks=round(window_beats * mid.ticks_per_beat),
        accent_gap=accent_gap,
    )
    out = type(mid)(type=mid.type, ticks_per_beat=mid.ticks_per_beat)
    out.tracks.extend(_rebuild(events) for events in merged)
    report["window_beats"] = window_beats
    return out, report
