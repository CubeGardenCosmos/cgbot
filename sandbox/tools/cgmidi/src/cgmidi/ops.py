"""MIDI 预处理:速度、移调、通道/音色/音库映射、静音与独奏、按秒裁剪。

纯内存变换,不做文件 IO。约定:
- 通道为 mido 原始值 0-15(CLI 的 1-16 在 cli.py 转换);音色与音库值为 0-127。
- 静音、独奏、强制音色/音库均以原始通道为准;通道改派(map_channel)最后应用。
- 鼓组通道(索引 9,即通道 10)不参与移调。
- 裁剪时间以原乐谱秒数计算(按原始速度标记换算为 tick),速度调整在裁剪之后应用。
- 无法无损表达的结果直接报错(如移调后音高越界),不静默截断。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from mido import Message, MetaMessage, MidiFile, MidiTrack, bpm2tempo, tempo2bpm

from cgmidi.errors import BAD_INPUT, OUT_OF_RANGE, PARSE_FAILED, CgmidiError

Event = tuple[int, Any]  # (绝对 tick, mido 消息)
Window = tuple[int, int | None]  # (起点 tick, 终点 tick;None 表示到结尾)

DRUM_CHANNEL = 9
DEFAULT_TEMPO = 500_000  # 120 BPM,MIDI 默认速度(微秒/拍)
MAX_TEMPO = 0xFFFFFF
BANK_CONTROLLERS = (0, 32)  # Bank Select MSB / LSB
NOTE_TYPES = frozenset({"note_on", "note_off"})
CHANNEL_TYPES = NOTE_TYPES | {
    "control_change",
    "program_change",
    "pitchwheel",
    "aftertouch",
    "polytouch",
}

# 同一 tick 内的排序:非音符事件先于音符;先关后开,保证同音高连音不被吞掉。
_RANK_OTHER, _RANK_NOTE_OFF, _RANK_NOTE_ON = 0, 1, 2


@dataclass
class Options:
    """预处理参数。通道为 0-15,音色与音库为 0-127。"""

    speed: float | None = None  # 速度倍率,>1 加快
    bpm: float | None = None  # 替换全部速度标记为固定 BPM
    transpose: int = 0  # 半音数,鼓组通道不移调
    mute: set[int] = field(default_factory=set)
    solo: set[int] = field(default_factory=set)  # 非空时只保留这些通道的音符
    map_channel: dict[int, int] = field(default_factory=dict)  # 源通道 -> 目标通道
    set_program: dict[int, int] = field(default_factory=dict)  # 强制音色
    set_bank: dict[int, tuple[int, int]] = field(default_factory=dict)  # 强制 (MSB, LSB)
    map_program: dict[int, int] = field(default_factory=dict)  # 音色号映射,所有通道
    start: float | None = None  # 裁剪起点(秒,原乐谱时间)
    end: float | None = None  # 裁剪终点(秒,原乐谱时间)


def transform(mid: MidiFile, opts: Options) -> MidiFile:
    """按 opts 生成新的 MidiFile;输入对象不被修改。"""
    _validate(opts)
    tpb = mid.ticks_per_beat
    tracks = [_absolute(track) for track in mid.tracks] or [[]]
    tmap = tempo_map(tracks)
    window = _window(opts, tracks, tmap, tpb)

    tracks = [_filter(events, opts) for events in tracks]
    if window is not None:
        tracks = [_trim(events, *window) for events in tracks]
    _apply_tempo(tracks, tmap, opts)
    _apply_program_bank(tracks, opts)
    if opts.map_channel:
        tracks = [_remap_channels(events, opts.map_channel) for events in tracks]

    out = MidiFile(type=mid.type, ticks_per_beat=tpb)
    out.tracks.extend(_rebuild(events) for events in tracks)
    return out


def describe(mid: MidiFile) -> dict[str, Any]:
    """MIDI 结构摘要,用于选择通道、核对速度与裁剪结果。通道号输出为 1-16。"""
    tpb = mid.ticks_per_beat
    tracks = [_absolute(track) for track in mid.tracks]
    tmap = tempo_map(tracks)
    total = max((tick for events in tracks for tick, _ in events), default=0)

    pitches: dict[int, list[int]] = {}
    programs: dict[int, set[int]] = {}
    banks: dict[int, set[int]] = {}
    track_info: list[dict[str, Any]] = []
    for index, events in enumerate(tracks):
        name = None
        count = 0
        for _tick, msg in events:
            if msg.type == "track_name" and name is None:
                name = msg.name
            elif msg.type == "note_on" and msg.velocity > 0:
                pitches.setdefault(msg.channel, []).append(msg.note)
                count += 1
            elif msg.type == "program_change":
                programs.setdefault(msg.channel, set()).add(msg.program)
            elif msg.type == "control_change" and msg.control == 0:
                banks.setdefault(msg.channel, set()).add(msg.value)
        track_info.append({"index": index, "name": name, "notes": count})

    channels: list[dict[str, Any]] = []
    for channel in sorted(set(pitches) | set(programs) | set(banks)):
        notes = pitches.get(channel, [])
        channels.append(
            {
                "channel": channel + 1,
                "notes": len(notes),
                "note_range": [min(notes), max(notes)] if notes else None,
                "programs": sorted(programs.get(channel, set())),
                "banks": sorted(banks.get(channel, set())),
            }
        )

    return {
        "format": mid.type,
        "ticks_per_beat": tpb,
        "duration_seconds": round(tick_to_seconds(total, tmap, tpb), 3),
        "notes": sum(len(notes) for notes in pitches.values()),
        "tempo": [
            {"seconds": round(tick_to_seconds(tick, tmap, tpb), 3), "bpm": round(tempo2bpm(v), 2)}
            for tick, v in tmap
        ],
        "channels": channels,
        "tracks": track_info,
    }


def tempo_map(tracks: list[list[Event]]) -> list[tuple[int, int]]:
    """有效速度表 [(tick, 微秒/拍)],首项必为 tick 0。同 tick 多个速度时后出现者生效。"""
    tempos = [
        (tick, msg.tempo)
        for events in tracks
        for tick, msg in events
        if msg.type == "set_tempo"
    ]
    for _, tempo in tempos:
        if tempo <= 0:
            raise CgmidiError(PARSE_FAILED, "速度标记为 0,无法换算时间")
    tempos.sort(key=lambda item: item[0])  # 稳定排序保留同 tick 的先后顺序
    if not tempos or tempos[0][0] > 0:
        tempos.insert(0, (0, DEFAULT_TEMPO))
    return tempos


def tick_to_seconds(tick: int, tmap: list[tuple[int, int]], tpb: int) -> float:
    seconds = 0.0
    for index, (t0, tempo) in enumerate(tmap):
        if index + 1 == len(tmap) or tick <= tmap[index + 1][0]:
            return seconds + (tick - t0) * tempo / (tpb * 1_000_000)
        seconds += (tmap[index + 1][0] - t0) * tempo / (tpb * 1_000_000)
    return seconds  # 不可达:tmap 非空且首项 tick 为 0


def _seconds_to_tick(seconds: float, tmap: list[tuple[int, int]], tpb: int) -> int:
    remaining = seconds
    for (t0, tempo), (t1, _) in zip(tmap, tmap[1:], strict=False):
        span = (t1 - t0) * tempo / (tpb * 1_000_000)
        if remaining <= span:
            break
        remaining -= span
    else:
        t0, tempo = tmap[-1]
    return t0 + round(remaining * tpb * 1_000_000 / tempo)


def _require(ok: bool, message: str) -> None:
    if not ok:
        raise CgmidiError(BAD_INPUT, message)


def _validate(opts: Options) -> None:
    _require(opts.speed is None or 0.1 <= opts.speed <= 10, "速度倍率须在 0.1-10 之间")
    _require(opts.bpm is None or 1 <= opts.bpm <= 999, "BPM 须在 1-999 之间")
    _require(opts.speed is None or opts.bpm is None, "速度倍率与固定 BPM 不能同时使用")
    _require(-48 <= opts.transpose <= 48, "移调须在 -48 到 48 半音之间")
    _require(opts.start is None or opts.start >= 0, "裁剪起点不能为负")
    _require(opts.end is None or opts.end > (opts.start or 0), "裁剪终点必须大于起点")
    channels = {
        *opts.mute,
        *opts.solo,
        *opts.map_channel,
        *opts.map_channel.values(),
        *opts.set_program,
        *opts.set_bank,
    }
    _require(all(0 <= ch <= 15 for ch in channels), "通道须在 1-16 之间")
    values = [
        *opts.set_program.values(),
        *opts.map_program,
        *opts.map_program.values(),
        *(v for pair in opts.set_bank.values() for v in pair),
    ]
    _require(all(0 <= v <= 127 for v in values), "音色与音库值须在 0-127 之间")


def _absolute(track: Iterable[Any]) -> list[Event]:
    tick = 0
    out: list[Event] = []
    for msg in track:
        tick += msg.time
        out.append((tick, msg))
    return out


def _window(
    opts: Options, tracks: list[list[Event]], tmap: list[tuple[int, int]], tpb: int
) -> Window | None:
    if opts.start is None and opts.end is None:
        return None
    total = max((tick for events in tracks for tick, _ in events), default=0)
    start = _seconds_to_tick(opts.start or 0.0, tmap, tpb)
    if start > 0 and start >= total:
        duration = tick_to_seconds(total, tmap, tpb)
        raise CgmidiError(BAD_INPUT, f"裁剪起点超出乐谱时长 {duration:.3f} 秒")
    end = None if opts.end is None else _seconds_to_tick(opts.end, tmap, tpb)
    return start, end


def _filter(events: list[Event], opts: Options) -> list[Event]:
    """静音/独奏、移调、音色映射;丢弃 end_of_track(保存时由 mido 重新补齐)。"""
    out: list[Event] = []
    for tick, msg in events:
        if msg.type == "end_of_track":
            continue
        if msg.type in NOTE_TYPES:
            ch = msg.channel
            if ch in opts.mute or (opts.solo and ch not in opts.solo):
                continue
            if opts.transpose and ch != DRUM_CHANNEL:
                note = msg.note + opts.transpose
                if not 0 <= note <= 127:
                    raise CgmidiError(
                        OUT_OF_RANGE,
                        f"移调后音高超出 0-127(通道 {ch + 1},tick {tick},"
                        f"原音高 {msg.note},移调 {opts.transpose})",
                    )
                msg = msg.copy(note=note)
        elif msg.type == "program_change" and msg.program in opts.map_program:
            msg = msg.copy(program=opts.map_program[msg.program])
        out.append((tick, msg))
    return out


def _trim(events: list[Event], start: int, end: int | None) -> list[Event]:
    """裁剪到 [start, end) 并平移到 0。

    起点之前的非音符事件(音色、控制器、速度等)折叠到 0,保留声部状态;
    起点时仍在发声的音符在 0 重新开始;终点时仍在发声的音符补 note_off。
    """
    split = next((i for i, (tick, _) in enumerate(events) if tick >= start), len(events))
    kept: list[Event] = []
    active: dict[tuple[int, int], list[Any]] = {}
    for _tick, msg in events[:split]:
        if msg.type in NOTE_TYPES:
            _track_note(active, msg)
        else:
            kept.append((0, msg))
    kept.extend((0, on) for stack in active.values() for on in stack)

    for tick, msg in events[split:]:
        if end is not None and tick >= end:
            break
        if msg.type in NOTE_TYPES:
            _track_note(active, msg)
        kept.append((tick - start, msg))

    if end is not None:
        kept.extend((end - start, _note_off(on)) for stack in active.values() for on in stack)
    return kept


def _track_note(active: dict[tuple[int, int], list[Any]], msg: Any) -> None:
    key = (msg.channel, msg.note)
    if msg.type == "note_on" and msg.velocity > 0:
        active.setdefault(key, []).append(msg)
    elif key in active:
        active[key].pop(0)
        if not active[key]:
            del active[key]


def _note_off(on: Any) -> Any:
    return Message("note_off", channel=on.channel, note=on.note, velocity=0)


def _scaled_tempo(tempo: int, speed: float) -> int:
    scaled = round(tempo / speed)
    if not 1 <= scaled <= MAX_TEMPO:
        raise CgmidiError(OUT_OF_RANGE, "速度倍率使速度标记超出 MIDI 可表示范围")
    return scaled


def _apply_tempo(tracks: list[list[Event]], tmap: list[tuple[int, int]], opts: Options) -> None:
    if opts.bpm is not None:
        for index, events in enumerate(tracks):
            tracks[index] = [(tick, msg) for tick, msg in events if msg.type != "set_tempo"]
        tracks[0].append((0, MetaMessage("set_tempo", tempo=bpm2tempo(opts.bpm))))
        return
    if opts.speed is None:
        return
    for events in tracks:
        for index, (tick, msg) in enumerate(events):
            if msg.type == "set_tempo":
                events[index] = (tick, msg.copy(tempo=_scaled_tempo(msg.tempo, opts.speed)))
    has_head_tempo = any(
        tick == 0 and msg.type == "set_tempo" for events in tracks for tick, msg in events
    )
    if not has_head_tempo:
        # 文件首个速度标记在 tick 0 之后(或没有速度标记):显式写入缩放后的默认速度
        head = _scaled_tempo(tmap[0][1], opts.speed)
        tracks[0].append((0, MetaMessage("set_tempo", tempo=head)))


def _overridden(msg: Any, opts: Options) -> bool:
    if msg.type == "program_change":
        return msg.channel in opts.set_program
    if msg.type == "control_change" and msg.control in BANK_CONTROLLERS:
        return msg.channel in opts.set_bank
    return False


def _apply_program_bank(tracks: list[list[Event]], opts: Options) -> None:
    """强制通道音色/音库:删除该通道全部对应消息,并在 tick 0 写入 Bank Select 再写 Program。"""
    if not (opts.set_program or opts.set_bank):
        return
    for index, events in enumerate(tracks):
        tracks[index] = [(tick, msg) for tick, msg in events if not _overridden(msg, opts)]
    head = tracks[0]
    for channel, (msb, lsb) in sorted(opts.set_bank.items()):
        head.append((0, Message("control_change", channel=channel, control=0, value=msb)))
        head.append((0, Message("control_change", channel=channel, control=32, value=lsb)))
    for channel, program in sorted(opts.set_program.items()):
        head.append((0, Message("program_change", channel=channel, program=program)))


def _remap_channels(events: list[Event], mapping: dict[int, int]) -> list[Event]:
    out: list[Event] = []
    for tick, msg in events:
        if msg.type in CHANNEL_TYPES and msg.channel in mapping:
            msg = msg.copy(channel=mapping[msg.channel])
        out.append((tick, msg))
    return out


def _rank(msg: Any) -> int:
    if msg.type == "note_on" and msg.velocity > 0:
        return _RANK_NOTE_ON
    if msg.type in NOTE_TYPES:
        return _RANK_NOTE_OFF
    return _RANK_OTHER


def _rebuild(events: list[Event]) -> MidiTrack:
    track = MidiTrack()
    prev = 0
    for tick, msg in sorted(events, key=lambda event: (event[0], _rank(event[1]))):
        track.append(msg.copy(time=tick - prev))
        prev = tick
    return track
