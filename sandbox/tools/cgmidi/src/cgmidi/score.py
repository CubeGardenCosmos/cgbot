"""小节边界与和声音高类分析,以及延音踏板(CC64)编辑。

乐谱事件始终是 MIDI:本模块只读音符/拍号/速度,或写 CC64,不把分析结果
塞进合成器。和弦名仅供人/LLM 阅读;踏板切分以音高类集合与低音为依据。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mido import Message, MidiFile

from cgmidi.errors import BAD_INPUT, CgmidiError
from cgmidi.ops import (
    DRUM_CHANNEL,
    NOTE_TYPES,
    Event,
    _absolute,
    _rebuild,
    tempo_map,
    tick_to_seconds,
)

_NOTE_NAMES = ("C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B")
_CHORD_TEMPLATES = (
    ("", frozenset((0, 4, 7))),
    ("m", frozenset((0, 3, 7))),
    ("7", frozenset((0, 4, 7, 10))),
    ("maj7", frozenset((0, 4, 7, 11))),
    ("m7", frozenset((0, 3, 7, 10))),
    ("dim", frozenset((0, 3, 6))),
    ("aug", frozenset((0, 4, 8))),
    ("sus4", frozenset((0, 5, 7))),
    ("sus2", frozenset((0, 2, 7))),
    ("5", frozenset((0, 7))),
)


@dataclass
class Note:
    start_tick: int
    end_tick: int
    pitch: int
    velocity: int
    channel: int
    track_index: int


@dataclass
class Bar:
    index: int
    start_tick: int
    end_tick: int
    numerator: int
    denominator: int
    pickup: bool


def extract_notes(tracks: list[list[Event]], channels: set[int] | None = None) -> list[Note]:
    """配对 note_on/note_off;同音高叠音按 FIFO 配对,未闭合音符延伸到文件末尾。"""
    active: dict[tuple[int, int], list[tuple[int, int, int]]] = {}
    notes: list[Note] = []
    total_tick = max((tick for events in tracks for tick, _msg in events), default=0)
    for track_index, events in enumerate(tracks):
        for tick, msg in events:
            if msg.type not in NOTE_TYPES:
                continue
            if channels is not None and msg.channel not in channels:
                continue
            key = (msg.channel, msg.note)
            if msg.type == "note_on" and msg.velocity > 0:
                active.setdefault(key, []).append((tick, msg.velocity, track_index))
            elif key in active and active[key]:
                start, velocity, start_track = active[key].pop(0)
                notes.append(Note(start, tick, msg.note, velocity, msg.channel, start_track))
                if not active[key]:
                    del active[key]
    for (channel, pitch), pending in active.items():
        for start, velocity, track_index in pending:
            notes.append(Note(start, max(start, total_tick), pitch, velocity, channel, track_index))
    notes.sort(key=lambda note: (note.start_tick, note.track_index, note.pitch))
    return notes


def time_signatures(tracks: list[list[Event]]) -> list[tuple[int, int, int]]:
    """有效拍号表,首项必为 tick 0;同 tick 后出现的拍号生效。"""
    values: dict[int, tuple[int, int]] = {}
    for events in tracks:
        for tick, msg in events:
            if msg.type == "time_signature":
                values[tick] = (msg.numerator, msg.denominator)
    if 0 not in values:
        values[0] = (4, 4)
    return [(tick, *values[tick]) for tick in sorted(values)]


def make_bars(tracks: list[list[Event]], notes: list[Note], tpb: int) -> list[Bar]:
    """按拍号生成小节;拍号提前出现则前一小节为不完整/弱起小节。

    首个内容不在 tick 0 时,将它之前的空拍视为弱起小节,而不是从第一个
    音符重新起算拍号。这样右手迟入、左手先行的钢琴谱仍与谱面小节对齐。
    """
    signatures = time_signatures(tracks)
    content_ticks = [
        tick
        for events in tracks
        for tick, msg in events
        if msg.type not in ("end_of_track", "track_name", "midi_port")
    ]
    last_tick = max([*(note.end_tick for note in notes), *content_ticks], default=0)
    if last_tick <= 0:
        return []
    bars: list[Bar] = []
    current = 0
    sig_index = 0
    while current < last_tick:
        while sig_index + 1 < len(signatures) and signatures[sig_index + 1][0] <= current:
            sig_index += 1
        _sig_tick, numerator, denominator = signatures[sig_index]
        length = max(1, round(numerator * tpb * 4 / denominator))
        nominal_end = current + length
        next_tick = signatures[sig_index + 1][0] if sig_index + 1 < len(signatures) else None
        end = next_tick if next_tick is not None and next_tick < nominal_end else nominal_end
        is_pickup = end - current < length
        if len(bars) == 0 and len(signatures) > 1 and signatures[1][0] == end:
            next_len = round(signatures[1][1] * tpb * 4 / signatures[1][2])
            if length < next_len:
                is_pickup = True
        bars.append(Bar(len(bars) + 1, current, end, numerator, denominator, is_pickup))
        current = end
        if next_tick is not None and next_tick <= current:
            sig_index += 1
    return bars


def guess_chord(bass_pitch: int | None, pitch_classes: set[int]) -> str | None:
    """用包含全部模板音且额外音最少的和弦命名;同分时低音优先。"""
    if bass_pitch is None or not pitch_classes:
        return None
    bass = bass_pitch % 12
    candidates: list[tuple[int, int, int, str]] = []
    for root in range(12):
        intervals = {(pc - root) % 12 for pc in pitch_classes}
        for suffix, template in _CHORD_TEMPLATES:
            if not template.issubset(intervals):
                continue
            extras = len(intervals - template)
            inversion = 0 if root == bass else 1
            label = f"{_NOTE_NAMES[root]}{suffix}"
            if inversion:
                label += f"/{_NOTE_NAMES[bass]}"
            candidates.append((extras, -len(template), inversion, label))
    if not candidates:
        return "/".join(_NOTE_NAMES[pc] for pc in sorted(pitch_classes))
    return min(candidates)[3]


def _fits_chord_template(pcs: set[int]) -> bool:
    if len(pcs) <= 1:
        return True
    if len(pcs) > 4:
        return False
    for root in range(12):
        intervals = {(pc - root) % 12 for pc in pcs}
        for _suffix, template in _CHORD_TEMPLATES:
            if intervals.issubset(template):
                return True
    return False


def harmony_segments(
    bar: Bar,
    notes: list[Note],
    tpb: int,
    tmap: list[tuple[int, int]],
) -> list[dict[str, Any]]:
    """以拍为观察窗,合并音高类未变化的窗口。

    有持续音时采用发声音,分解和弦则累积本段已出现的音高类。低音的音高类
    改变且集合未包含于当前段时切分;仅低音回落也切分,避免 G 回到 C 被
    误并入前一和弦。返回的 pitch_classes 始终是实际观察值,而非猜测补音。
    """
    active = [
        note
        for note in notes
        if note.channel != DRUM_CHANNEL
        and note.start_tick < bar.end_tick
        and note.end_tick > bar.start_tick
    ]
    if not active:
        return [_segment_dict(bar.start_tick, bar.end_tick, set(), None, tpb, tmap)]
    beat = max(1, round(tpb * 4 / bar.denominator))
    windows: list[tuple[int, int, list[Note]]] = []
    start = bar.start_tick
    while start < bar.end_tick:
        end = min(bar.end_tick, start + beat)
        sounding = [note for note in active if note.start_tick < end and note.end_tick > start]
        if not sounding:
            # 极短音符仍属于该窗口,不能因 quantized 边界消失。
            sounding = [note for note in active if start <= note.start_tick < end]
        windows.append((start, end, sounding))
        start = end

    raw: list[tuple[int, int, set[int], int | None]] = []
    for start, end, sounding in windows:
        if not sounding:
            if raw:
                previous_start, _previous_end, pcs, bass = raw[-1]
                raw[-1] = (previous_start, end, pcs, bass)
            continue
        pcs = {note.pitch % 12 for note in sounding}
        bass = min(note.pitch for note in sounding)
        if raw:
            _seg_start, _seg_end, accumulated, previous_bass = raw[-1]
            bass_drop = (
                previous_bass is not None and bass < previous_bass and not pcs.issubset(accumulated)
            )
            combined = accumulated | pcs
            chord_fits = _fits_chord_template(combined)
            changed = bass_drop or (not chord_fits and not pcs.issubset(accumulated))
            if changed:
                raw.append((start, end, set(pcs), bass))
            else:
                raw[-1] = (
                    _seg_start,
                    end,
                    combined,
                    min(previous_bass, bass) if previous_bass is not None else bass,
                )
        else:
            raw.append((start, end, set(pcs), bass))
    return [_segment_dict(start, end, pcs, bass, tpb, tmap) for start, end, pcs, bass in raw]


def _segment_dict(
    start: int,
    end: int,
    pcs: set[int],
    bass: int | None,
    tpb: int,
    tmap: list[tuple[int, int]],
) -> dict[str, Any]:
    return {
        "start_tick": start,
        "end_tick": end,
        "start_seconds": round(tick_to_seconds(start, tmap, tpb), 3),
        "end_seconds": round(tick_to_seconds(end, tmap, tpb), 3),
        "pitch_classes": sorted(pcs),
        "pitch_class_names": [_NOTE_NAMES[pc] for pc in sorted(pcs)],
        "bass_pitch": bass,
        "chord": guess_chord(bass, pcs),
    }


def analyze_bars(
    mid: MidiFile,
    *,
    channels: set[int] | None = None,
) -> dict[str, Any]:
    """列出小节和每段实际音高类集合,通道输出为 1-16。"""
    tracks = [_absolute(track) for track in mid.tracks]
    tmap = tempo_map(tracks)
    selected = channels if channels is not None else set(range(16)) - {DRUM_CHANNEL}
    notes = extract_notes(tracks, selected)
    bars = make_bars(tracks, notes, mid.ticks_per_beat)
    output_bars: list[dict[str, Any]] = []
    for bar in bars:
        bar_notes = [note for note in notes if bar.start_tick <= note.start_tick < bar.end_tick]
        segments = harmony_segments(bar, notes, mid.ticks_per_beat, tmap)
        output_bars.append(
            {
                "index": bar.index,
                "start_tick": bar.start_tick,
                "end_tick": bar.end_tick,
                "start_seconds": round(
                    tick_to_seconds(bar.start_tick, tmap, mid.ticks_per_beat), 3
                ),
                "end_seconds": round(tick_to_seconds(bar.end_tick, tmap, mid.ticks_per_beat), 3),
                "time_signature": f"{bar.numerator}/{bar.denominator}",
                "pickup": bar.pickup,
                "notes": len(bar_notes),
                "harmony": segments,
            }
        )
    return {
        "total_bars": len(bars),
        "total_notes": len(notes),
        "time_signatures": [f"{num}/{den}" for _tick, num, den in time_signatures(tracks)],
        "channels": sorted(channel + 1 for channel in selected),
        "bars": output_bars,
    }


def _arpeggiated(bar: Bar, notes: list[Note]) -> bool:
    """至少三个错开的 onset,且一半以上音符不与别的音同时起奏。"""
    selected = [
        note
        for note in notes
        if note.channel != DRUM_CHANNEL and bar.start_tick <= note.start_tick < bar.end_tick
    ]
    if len(selected) < 3:
        return False
    counts: dict[int, int] = {}
    for note in selected:
        counts[note.start_tick] = counts.get(note.start_tick, 0) + 1
    staggered = sum(count for count in counts.values() if count == 1)
    return len(counts) >= 3 and staggered * 2 >= len(selected)


def select_pedal_channels(tracks: list[list[Event]]) -> set[int]:
    """优先选择 GM 键盘(program 0-7);无音色标记时选音符最多的非鼓通道。"""
    programs: dict[int, set[int]] = {}
    counts: dict[int, int] = {}
    for events in tracks:
        for _tick, msg in events:
            if msg.type == "program_change":
                programs.setdefault(msg.channel, set()).add(msg.program)
            elif msg.type == "note_on" and msg.velocity > 0:
                counts[msg.channel] = counts.get(msg.channel, 0) + 1
    keyboards = {
        channel
        for channel, values in programs.items()
        if channel != DRUM_CHANNEL and any(program <= 7 for program in values)
    }
    if keyboards:
        return keyboards
    melodic = {channel: count for channel, count in counts.items() if channel != DRUM_CHANNEL}
    if melodic:
        return {max(melodic, key=lambda channel: melodic[channel])}
    return set()


def pedal_plan(
    bars: list[Bar], notes: list[Note], tpb: int, tmap: list[tuple[int, int]], mode: str
) -> tuple[list[int], str, int]:
    """返回换踏板 tick、auto 实际采用的模式与和声段数。"""
    if mode not in ("bar", "chord", "auto"):
        raise CgmidiError(BAD_INPUT, "踏板模式须为 bar/chord/auto")
    ticks: list[int] = []
    segment_count = 0
    automatic: list[str] = []
    for bar in bars:
        segments = harmony_segments(bar, notes, tpb, tmap)
        segment_count += len(segments)
        selected = mode
        if mode == "auto":
            selected = "chord" if _arpeggiated(bar, notes) else "bar"
            automatic.append(selected)
        if selected == "bar":
            ticks.append(bar.start_tick)
        else:
            ticks.extend(segment["start_tick"] for segment in segments)
    resolved = (
        mode
        if mode != "auto"
        else ("chord" if automatic and all(item == "chord" for item in automatic) else "bar")
    )
    return sorted(set(ticks)), resolved, segment_count


def apply_pedal(
    mid: MidiFile,
    *,
    mode: str = "auto",
    channels: set[int] | None = None,
    replace: bool = False,
    gap_ticks: int | None = None,
) -> tuple[MidiFile | None, dict[str, Any]]:
    """添加或替换延音踏板;已有 CC64 默认原样保留且不写输出。

    换段时在边界前释放、边界后按下;小段内自动缩短间隔,避免负 delta。
    """
    if mode not in ("bar", "chord", "auto"):
        raise CgmidiError(BAD_INPUT, "踏板模式须为 bar/chord/auto")
    if gap_ticks is not None and gap_ticks <= 0:
        raise CgmidiError(BAD_INPUT, "--gap-ticks 须为正整数")
    tracks = [_absolute(track) for track in mid.tracks]
    selected = channels if channels is not None else select_pedal_channels(tracks)
    existing = sum(
        msg.type == "control_change" and msg.control == 64 and msg.channel in selected
        for events in tracks
        for _tick, msg in events
    )
    if existing and not replace:
        return None, {
            "status": "already_pedaled",
            "mode": mode,
            "channels": sorted(channel + 1 for channel in selected),
            "existing_pedal_events": existing,
            "notes_changed": 0,
            "velocities_changed": 0,
        }
    notes = extract_notes(tracks, selected)
    bars = make_bars(tracks, notes, mid.ticks_per_beat)
    tmap = tempo_map(tracks)
    changes, resolved_mode, segment_count = pedal_plan(bars, notes, mid.ticks_per_beat, tmap, mode)
    gap = gap_ticks if gap_ticks is not None else max(1, mid.ticks_per_beat // 16)
    end = max([bars[-1].end_tick if bars else 0, *(note.end_tick for note in notes)])
    generated: list[tuple[int, int]] = []
    previous_press = -1
    for change in changes:
        if not generated:
            press = min(gap, max(0, end - 1))
            generated.append((press, 127))
            previous_press = press
            continue
        effective_gap = min(gap, max(1, (change - previous_press) // 4))
        release = max(previous_press + 1, change - effective_gap)
        press = min(end, change + effective_gap)
        if press <= release:
            continue
        generated.extend(((release, 0), (press, 127)))
        previous_press = press
    if generated:
        generated.append((max(previous_press + 1, end), 0))

    # 每个通道选一个音符最多的轨道写控制器;避免同 tick 多轨 CC 的顺序歧义。
    destinations: dict[int, int] = {}
    for channel in selected:
        counts = [
            sum(
                msg.type == "note_on" and msg.velocity > 0 and msg.channel == channel
                for _tick, msg in events
            )
            for events in tracks
        ]
        destinations[channel] = counts.index(max(counts)) if any(counts) else 0
    out = MidiFile(type=mid.type, ticks_per_beat=mid.ticks_per_beat)
    for index, events in enumerate(tracks):
        kept = [
            (tick, msg.copy())
            for tick, msg in events
            if not (
                replace
                and msg.type == "control_change"
                and msg.control == 64
                and msg.channel in selected
            )
        ]
        for channel, destination in destinations.items():
            if destination == index:
                kept.extend(
                    (
                        tick,
                        Message("control_change", channel=channel, control=64, value=value),
                    )
                    for tick, value in generated
                )
        out.tracks.append(_rebuild(kept))
    return out, {
        "status": "ok",
        "mode": mode,
        "resolved_mode": resolved_mode,
        "channels": sorted(channel + 1 for channel in selected),
        "pedal_events": len(generated) * len(selected),
        "removed_pedal_events": existing if replace else 0,
        "existing_pedal_events": existing,
        "bars": len(bars),
        "harmony_segments": segment_count,
        "gap_ticks": gap,
        "notes_changed": 0,
        "velocities_changed": 0,
    }
