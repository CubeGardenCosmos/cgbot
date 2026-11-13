"""MusicXML/MXL -> 每声部一个 MIDI 文件。

行为移植自 sandbox/tools/cgmusic/src/musicxml.cpp(解析与 SMF 编码规则),
差异仅在输出布局:每个声部独立文件、通道固定 0(鼓 9)、每个文件都写
完整指挥速度表/拍号(核心编码只把指挥轨写在第一轨)。拨奏切换按共享契约
编码为程序变更:拨奏 = GM 45(Pizzicato Strings),弓奏 = 原程序。
"""

from __future__ import annotations

import io
import math
import re
import zipfile
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

import mido

PPQ = 480
MAX_NOTES = 500_000
MAX_REPEAT_WORK = 1_000_000
MAX_REPEAT_TIMES = 1000
MAX_DIVISIONS = 1_000_000
MAX_DURATION = 1_000_000_000
MAX_TICKS = 0x0FFFFFFF
GRACE_TICKS = PPQ // 16
DRUM_CHANNEL = 9
DRUM_PITCH = 38
DEFAULT_TEMPO_MICROS = 500_000
DEFAULT_VELOCITY = 80  # mf
MAX_INPUT_BYTES = 64 * 1024 * 1024


class ScoreError(ValueError):
    """坏乐谱输入。"""


def _fail(message: str) -> None:
    raise ScoreError(message)


def _trim(text: str | None) -> str:
    return (text or "").strip()


def _child_value(node: ET.Element, name: str) -> str:
    child = node.find(name)
    return _trim(child.text) if child is not None else ""


def _parse_int(text: str, message: str) -> int:
    text = text.strip()
    if not re.fullmatch(r"[+-]?\d+", text):
        _fail(message)
    return int(text)


def _parse_beats(text: str) -> int:
    return sum(_parse_int(piece, "Invalid time beats") for piece in text.split("+"))


def _parse_number(text: str, message: str) -> float:
    try:
        value = float(text.strip())
    except ValueError:
        raise ScoreError(message) from None
    if not math.isfinite(value):
        _fail(message)
    return value


DYNAMICS = {"ppp": 28, "pp": 40, "p": 52, "mp": 64, "mf": 80, "f": 96, "ff": 112, "fff": 124}

# 最长匹配优先(与 C++ 一致)
PROGRAMS = [
    ("piano", 0),
    ("keyboard", 0),
    ("organ", 19),
    ("guitar", 24),
    ("bass", 32),
    ("violin", 40),
    ("viola", 41),
    ("cello", 42),
    ("contrabass", 43),
    ("string", 48),
    ("choir", 52),
    ("voice", 52),
    ("trumpet", 56),
    ("trombone", 57),
    ("tuba", 58),
    ("horn", 60),
    ("sax", 65),
    ("oboe", 68),
    ("english horn", 69),
    ("bassoon", 70),
    ("contrabassoon", 70),
    ("clarinet", 71),
    ("flute", 73),
    ("recorder", 74),
    ("vibraphone", 11),
    ("marimba", 12),
    ("xylophone", 13),
]
DRUM_WORDS = ["drum", "percussion", "鼓", "打击"]

BEAT_UNITS = {
    "maxima": 32.0,
    "long": 16.0,
    "breve": 8.0,
    "whole": 4.0,
    "half": 2.0,
    "quarter": 1.0,
    "eighth": 0.5,
    "16th": 0.25,
    "32nd": 0.125,
    "64th": 0.0625,
    "128th": 0.03125,
    "256th": 0.015625,
}


def guess_program(lowered: str) -> int:
    best, best_len = 0, 0
    for name, program in PROGRAMS:
        if len(name) > best_len and name in lowered:
            best, best_len = program, len(name)
    return best


def is_drum_name(lowered: str) -> bool:
    return any(word in lowered for word in DRUM_WORDS)


def velocity_from_percent(percent: float) -> int:
    clamped = min(max(percent, 0.0), 200.0)
    return min(max(int(round(clamped * 1.27)), 1), 127)


STEP_SEMITONES = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}


def _step_semitones(step: str) -> int:
    if step not in STEP_SEMITONES:
        _fail("Invalid pitch step")
    return STEP_SEMITONES[step]


def _midi_pitch(octave: int, step: int, offset: int) -> int:
    if octave < -1 or octave > 10:
        _fail("Note pitch out of MIDI range")
    value = (octave + 1) * 12 + step + offset
    if value < 0 or value > 127:
        _fail("Note pitch out of MIDI range")
    return value


@dataclass
class _Declared:
    name: str = ""
    program: int | None = None
    channel: int | None = None
    drum: bool = False
    list_rank: int = -1


@dataclass
class _Note:
    start: int
    end: int
    pitch: int
    velocity: int


@dataclass
class _TempoEv:
    tick: int
    micros: int


@dataclass
class _SigEv:
    tick: int
    beats: int
    beat_type: int


@dataclass
class _PizzEv:
    tick: int
    pizz: bool


@dataclass
class _PartOut:
    name: str = ""
    drum: bool = False
    program: int = 0
    notes: list[_Note] = field(default_factory=list)
    keys: list[tuple[int, int, bool]] = field(default_factory=list)
    tempos: list[_TempoEv] = field(default_factory=list)
    sigs: list[_SigEv] = field(default_factory=list)
    pizz_events: list[_PizzEv] = field(default_factory=list)


@dataclass
class _State:
    divisions: int = 1
    beats: int = 4
    beat_type: int = 4
    transpose: int = 0
    velocity: int = DEFAULT_VELOCITY
    pizz: bool = False
    open_ties: dict = field(default_factory=dict)


def _read_mxl(data: bytes) -> bytes:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ScoreError("Invalid compressed MusicXML score") from None
    names = archive.namelist()
    total = sum(info.file_size for info in archive.infolist())
    if total > MAX_INPUT_BYTES:
        _fail("Compressed score is too large")
    score = None
    if "META-INF/container.xml" in names:
        container = ET.fromstring(archive.read("META-INF/container.xml"))
        for rootfile in container.iter("rootfile"):
            path = _trim(rootfile.get("full-path"))
            media = _trim(rootfile.get("media-type"))
            if not path or (media and "musicxml" not in media):
                continue
            if path in names:
                score = path
                break
    if score is None:
        candidates = [
            n
            for n in names
            if (n.lower().endswith((".xml", ".musicxml"))) and not n.startswith("META-INF/")
        ]
        if len(candidates) != 1:
            _fail("Compressed score has no unique MusicXML file")
        score = candidates[0]
    return archive.read(score)


def _read_declared(part: ET.Element, drum_pitch: dict) -> _Declared:
    declared = _Declared(name=_child_value(part, "part-name"))
    for instrument in part:
        if instrument.tag not in ("midi-instrument", "score-instrument"):
            continue
        instrument_id = _trim(instrument.get("id"))
        for item in instrument:
            if item.tag == "midi-program":
                value = _parse_int(_trim(item.text), "Invalid midi-program")
                if not 1 <= value <= 128:
                    _fail("Invalid midi-program")
                if declared.program is None:
                    declared.program = value - 1
            elif item.tag == "midi-channel":
                value = _parse_int(_trim(item.text), "Invalid midi-channel")
                if not 1 <= value <= 16:
                    _fail("Invalid midi-channel")
                if value == 10:
                    declared.drum = True
                elif declared.channel is None:
                    declared.channel = value - 1
            elif item.tag == "midi-unpitched":
                value = _parse_int(_trim(item.text), "Invalid midi-unpitched")
                if not 1 <= value <= 128:
                    _fail("Invalid midi-unpitched")
                declared.drum = True
                if instrument_id:
                    drum_pitch.setdefault(instrument_id, value - 1)
            elif item.tag == "percussion":
                declared.drum = True
    if is_drum_name(declared.name.lower()):
        declared.drum = True
    return declared


def _read_declarations(root: ET.Element) -> tuple[dict, dict]:
    parts: dict[str, _Declared] = {}
    drum_pitch: dict[str, int] = {}
    part_list = root.find("part-list")
    rank = 0
    if part_list is not None:
        for score_part in part_list:
            if score_part.tag != "score-part":
                continue
            declared = _read_declared(score_part, drum_pitch)
            declared.list_rank = rank
            rank += 1
            parts[_trim(score_part.get("id"))] = declared
    return parts, drum_pitch


def _scale_divisions(value: int, st: _State) -> int:
    return (value * PPQ + st.divisions // 2) // st.divisions


def _duration_ticks(node: ET.Element, st: _State) -> int:
    duration = node.find("duration")
    if duration is None:
        _fail("Missing duration")
    value = _parse_int(_trim(duration.text), "Invalid duration")
    if value < 0 or value > MAX_DURATION:
        _fail("Invalid duration")
    return _scale_divisions(value, st)


def _offset_ticks(direction: ET.Element, st: _State) -> int:
    offset = direction.find("offset")
    if offset is None:
        return 0
    value = _parse_int(_trim(offset.text), "Invalid offset")
    if abs(value) > MAX_DURATION:
        _fail("Invalid offset")
    return _scale_divisions(value, st) if value >= 0 else -_scale_divisions(-value, st)


def _ending_numbers(text: str) -> list[int]:
    return [int(n) for n in re.findall(r"\d+", text)]


@dataclass
class _Marks:
    forward: bool = False
    backward_times: int = 0
    ending_start: bool = False
    ending_stop: bool = False
    ending_numbers: list = field(default_factory=list)


def _marks_of(measure: ET.Element) -> _Marks:
    marks = _Marks()
    for barline in measure:
        if barline.tag != "barline":
            continue
        for item in barline:
            if item.tag == "repeat":
                direction = _trim(item.get("direction")).lower()
                if direction == "forward":
                    marks.forward = True
                elif direction == "backward":
                    times = _trim(item.get("times"))
                    count = 2 if not times else _parse_int(times, "Invalid repeat times")
                    if count > MAX_REPEAT_TIMES:
                        _fail("Repeat count is too large")
                    marks.backward_times = max(count, 1)
            elif item.tag == "ending":
                kind = _trim(item.get("type")).lower()
                if kind == "start":
                    marks.ending_start = True
                    marks.ending_numbers = _ending_numbers(_trim(item.get("number")))
                elif kind in ("stop", "discontinue"):
                    marks.ending_stop = True
    return marks


def _repeat_plan(marks: list[_Marks]) -> list[int]:
    plan: list[int] = []
    count = len(marks)
    active: int | None = None
    section_begin = 0
    pass_no = 1
    steps = 0

    def close_if_done(j: int) -> None:
        nonlocal section_begin, active, pass_no
        if marks[j].ending_stop and (j + 1 >= count or not marks[j + 1].ending_start):
            section_begin, active, pass_no = j + 1, None, 1

    i = 0
    while i < count:
        steps += 1
        if steps > MAX_REPEAT_WORK:
            _fail("Repeat expansion is too large")
        m = marks[i]
        if m.forward and active != i:
            active, pass_no = i, 1
        if m.ending_start and m.ending_numbers and pass_no not in m.ending_numbers:
            stop = i
            while stop < count and not marks[stop].ending_stop:
                stop += 1
            if stop < count:
                close_if_done(stop)
                i = stop + 1
                continue
        plan.append(i)
        if m.backward_times > 0:
            if active is None:
                active, pass_no = section_begin, 1
            if pass_no < m.backward_times:
                pass_no += 1
                i = active
                continue
            section_begin, active, pass_no = i + 1, None, 1
            i += 1
            continue
        close_if_done(i)
        i += 1
    return plan


def _has_tie(note: ET.Element, kind: str) -> bool:
    for item in note:
        if item.tag == "tie" and item.get("type") == kind:
            return True
        if item.tag == "notations":
            for tied in item:
                if tied.tag == "tied" and tied.get("type") == kind:
                    return True
    return False


def _pitch_of(note: ET.Element, drum: bool, transpose: int, drum_pitch: dict) -> int:
    if drum:
        instrument = note.find("instrument")
        if instrument is not None:
            instrument_id = _trim(instrument.get("id"))
            if instrument_id in drum_pitch:
                return drum_pitch[instrument_id]
        return DRUM_PITCH
    pitch = note.find("pitch")
    if pitch is not None:
        step = _step_semitones(_child_value(pitch, "step"))
        octave = _parse_int(_child_value(pitch, "octave"), "Invalid pitch octave")
        alter = pitch.find("alter")
        alter_value = 0.0
        if alter is not None:
            alter_value = _parse_number(_trim(alter.text), "Invalid pitch alter")
        if abs(alter_value) > 4.0:
            _fail("Invalid pitch alter")
        return _midi_pitch(octave, step, int(round(alter_value)) + transpose)
    unpitched = note.find("unpitched")
    if unpitched is not None:
        step_text = _child_value(unpitched, "display-step")
        step = 0 if not step_text else _step_semitones(step_text)
        octave_text = _child_value(unpitched, "display-octave")
        octave = 4 if not octave_text else _parse_int(octave_text, "Invalid display-octave")
        return _midi_pitch(octave, step, 0)
    _fail("Note has no pitch")


def _note_velocity(note: ET.Element, st: _State) -> int:
    dynamics = _trim(note.get("dynamics"))
    if not dynamics:
        return st.velocity
    return velocity_from_percent(_parse_number(dynamics, "Invalid note dynamics"))


def _add_tempo(out: _PartOut, tick: int, bpm: float) -> None:
    if not bpm > 0.0:
        _fail("Tempo must be positive")
    micros = round(60_000_000.0 / bpm)
    if micros < 1 or micros > 16_777_215:
        _fail("Tempo out of range")
    out.tempos.append(_TempoEv(tick, micros))


def _metronome_bpm(metronome: ET.Element) -> float | None:
    per_minute = metronome.find("per-minute")
    if per_minute is None:
        return None
    count = _parse_number(_trim(per_minute.text), "Invalid per-minute")
    unit_name = _child_value(metronome, "beat-unit").lower()
    quarters = 1.0
    if unit_name:
        if unit_name not in BEAT_UNITS:
            _fail("Invalid beat-unit")
        quarters = BEAT_UNITS[unit_name]
    dots = sum(1 for item in metronome if item.tag == "beat-unit-dot")
    return count * quarters * (2.0 - math.pow(0.5, dots))


def _set_pizz(new_pizz: bool, tick: int, st: _State, out: _PartOut) -> None:
    if st.pizz != new_pizz:
        st.pizz = new_pizz
        out.pizz_events.append(_PizzEv(tick, new_pizz))


def _apply_sound(sound: ET.Element, tick: int, st: _State, out: _PartOut) -> float | None:
    tempo = None
    tempo_text = _trim(sound.get("tempo"))
    if tempo_text:
        tempo = _parse_number(tempo_text, "Invalid sound tempo")
    dynamics_text = _trim(sound.get("dynamics"))
    if dynamics_text:
        st.velocity = velocity_from_percent(_parse_number(dynamics_text, "Invalid sound dynamics"))
    pizz_attr = _trim(sound.get("pizzicato"))
    if pizz_attr == "yes":
        _set_pizz(True, tick, st, out)
    elif pizz_attr == "no":
        _set_pizz(False, tick, st, out)
    return tempo


def _handle_direction(direction: ET.Element, tick: int, st: _State, out: _PartOut) -> None:
    metronome = None
    sound_tempo = None
    for item in direction:
        if item.tag == "direction-type":
            for kind in item:
                if kind.tag == "dynamics":
                    for mark in kind:
                        velocity = DYNAMICS.get(mark.tag, -1)
                        if velocity > 0:
                            st.velocity = velocity
                elif kind.tag == "metronome":
                    metronome = _metronome_bpm(kind)
                elif kind.tag == "words":
                    word = _trim(kind.text).lower()
                    if word in ("pizz.", "pizz"):
                        _set_pizz(True, tick, st, out)
                    elif word in ("arco.", "arco"):
                        _set_pizz(False, tick, st, out)
        elif item.tag == "sound":
            sound_tempo = _apply_sound(item, tick, st, out)
    if metronome is not None:
        _add_tempo(out, tick, metronome)
    elif sound_tempo is not None:
        _add_tempo(out, tick, sound_tempo)


def _apply_attributes(attributes: ET.Element, tick: int, st: _State, out: _PartOut) -> None:
    divisions = attributes.find("divisions")
    if divisions is not None:
        value = _parse_int(_trim(divisions.text), "Invalid divisions")
        if value < 1 or value > MAX_DIVISIONS:
            _fail("Invalid divisions")
        st.divisions = value
    time = attributes.find("time")
    if time is not None:
        beats = time.find("beats")
        beat_type = time.find("beat-type")
        if beats is not None and beat_type is not None:
            count = _parse_beats(_trim(beats.text))
            unit = _parse_int(_trim(beat_type.text), "Invalid beat-type")
            if count < 1 or count > 255 or not (1 <= unit <= 128 and unit & (unit - 1) == 0):
                _fail("Invalid time signature")
            st.beats, st.beat_type = count, unit
            out.sigs.append(_SigEv(tick, count, unit))
    key = attributes.find("key")
    if key is not None:
        fifths = _parse_int(_child_value(key, "fifths"), "Invalid key fifths")
        if fifths < -7 or fifths > 7:
            _fail("Invalid key fifths")
        minor = _child_value(key, "mode").lower() == "minor"
        out.keys.append((tick, fifths, minor))
    transpose = attributes.find("transpose")
    if transpose is not None:
        chromatic = _child_value(transpose, "chromatic")
        octave = _child_value(transpose, "octave-change")
        semitones = 0 if not chromatic else _parse_int(chromatic, "Invalid transpose")
        octaves = 0 if not octave else _parse_int(octave, "Invalid transpose")
        if not (-48 <= semitones <= 48 and -4 <= octaves <= 4):
            _fail("Invalid transpose")
        st.transpose = semitones + 12 * octaves


def _play_note(
    note: ET.Element,
    cursor: int,
    last_onset: int,
    st: _State,
    out: _PartOut,
    drum_pitch: dict,
    total: list[int],
) -> tuple[int, int]:
    grace = note.find("grace") is not None
    chord = note.find("chord") is not None
    rest = note.find("rest") is not None
    cue = note.find("cue") is not None
    length = GRACE_TICKS if grace else _duration_ticks(note, st)
    start = last_onset if (chord and not grace) else cursor

    for notations in note:
        if notations.tag != "notations":
            continue
        for tech in notations:
            if tech.tag == "technical" and tech.find("pizzicato") is not None:
                _set_pizz(True, start, st, out)

    if not rest and not cue:
        pitch = _pitch_of(note, out.drum, st.transpose, drum_pitch)
        velocity = _note_velocity(note, st)
        end = start + max(1, length)
        tie_start = not grace and _has_tie(note, "start")
        tie_stop = not grace and _has_tie(note, "stop")
        voice = _child_value(note, "voice") or "1"
        key = (voice, pitch)
        merged = False
        if tie_stop and key in st.open_ties:
            open_note = out.notes[st.open_ties[key]]
            open_note.end = max(open_note.end, end)
            if not tie_start:
                del st.open_ties[key]
            merged = True
        if not merged:
            if len(out.notes) >= MAX_NOTES:
                _fail("Score has too many notes")
            out.notes.append(_Note(start, end, pitch, velocity))
            if tie_start:
                st.open_ties[key] = len(out.notes) - 1
            total[0] += 1
    if not grace and not chord:
        last_onset = cursor
        cursor += length
    return cursor, last_onset


def _play_measure(
    measure: ET.Element, start: int, st: _State, out: _PartOut, drum_pitch: dict, total: list[int]
) -> int:
    cursor = start
    furthest = start
    last_onset = start
    for item in measure:
        if item.tag == "attributes":
            _apply_attributes(item, cursor, st, out)
        elif item.tag == "direction":
            _handle_direction(item, cursor + _offset_ticks(item, st), st, out)
        elif item.tag == "sound":
            tempo = _apply_sound(item, cursor, st, out)
            if tempo is not None:
                _add_tempo(out, cursor, tempo)
        elif item.tag == "backup":
            cursor = max(start, cursor - _duration_ticks(item, st))
        elif item.tag == "forward":
            cursor += _duration_ticks(item, st)
        elif item.tag == "note":
            cursor, last_onset = _play_note(item, cursor, last_onset, st, out, drum_pitch, total)
        furthest = max(furthest, cursor)
        if furthest > MAX_TICKS:
            _fail("Score is too long")
    return furthest


def _play_part(
    out: _PartOut, measures: list[ET.Element], drum_pitch: dict, total: list[int]
) -> None:
    marks = [_marks_of(m) for m in measures]
    plan = _repeat_plan(marks)
    st = _State()
    now = 0
    for index in plan:
        end = _play_measure(measures[index], now, st, out, drum_pitch, total)
        length = end - now
        if length <= 0:
            length = st.beats * 4 * PPQ // st.beat_type
        now += length
        if now > MAX_TICKS:
            _fail("Score is too long")


def _settle_by_tick(items: list) -> list:
    items = sorted(items, key=lambda e: e.tick)  # stable
    return [e for i, e in enumerate(items) if i + 1 == len(items) or items[i + 1].tick != e.tick]


def parse_score(data: bytes, filename: str):
    """返回 (parts: list[_PartOut], tempos: list[_TempoEv], sigs: list[_SigEv])。"""
    if data[:4] == b"PK\x03\x04" or filename.lower().endswith(".mxl"):
        data = _read_mxl(data)
    if len(data) > MAX_INPUT_BYTES:
        _fail("MusicXML score is too large")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ScoreError(f"Invalid MusicXML document: {exc}") from exc

    sources: list[tuple[str, list[ET.Element]]] = []
    if root.tag == "score-timewise":
        index: dict[str, int] = {}
        for measure in root:
            if measure.tag != "measure":
                continue
            for part in measure:
                if part.tag != "part":
                    continue
                pid = _trim(part.get("id"))
                if pid not in index:
                    index[pid] = len(sources)
                    sources.append((pid, []))
                sources[index[pid]][1].append(part)
    elif root.tag == "score-partwise":
        for part in root:
            if part.tag != "part":
                continue
            sources.append((_trim(part.get("id")), [m for m in part if m.tag == "measure"]))
    else:
        _fail("Expected a score-partwise MusicXML document")
    if not sources:
        _fail("Score has no parts")
    if len({pid for pid, _ in sources}) != len(sources):
        _fail("Duplicate part id")

    declared, drum_pitch = _read_declarations(root)
    parts: list[_PartOut] = []
    total = [0]
    for pid, measures in sources:
        decl = declared.get(pid)
        out = _PartOut()
        out.name = decl.name if decl and decl.name else pid
        out.drum = (decl.drum if decl else False) or is_drum_name(out.name.lower())
        out.program = (
            decl.program if (decl and decl.program is not None) else guess_program(out.name.lower())
        )
        _play_part(out, measures, drum_pitch, total)
        parts.append(out)

    tempos = [_TempoEv(0, DEFAULT_TEMPO_MICROS)]
    sigs = [_SigEv(0, 4, 4)]
    for part in parts:
        tempos.extend(part.tempos)
        sigs.extend(part.sigs)
    return parts, _settle_by_tick(tempos), _settle_by_tick(sigs)


def part_to_midi(
    part: _PartOut, channel: int, tempos: list[_TempoEv], sigs: list[_SigEv]
) -> mido.MidiFile:
    """编码单声部 SMF(format 1,单轨,含完整速度/拍号表)。

    同刻顺序与 C++ 一致:元信息 < note-off < note-on;拨奏程序变更在元信息后、
    音符前。拨奏 = 程序 45,弓奏 = 声部原程序。
    """
    mid = mido.MidiFile(type=1, ticks_per_beat=PPQ)
    track = mido.MidiTrack()
    mid.tracks.append(track)
    track.append(mido.MetaMessage("track_name", name=part.name, time=0))
    if not part.drum:
        track.append(mido.Message("program_change", channel=channel, program=part.program, time=0))

    # (tick, prio, seq, message);prio: 0 元信息, 1 程序变更, 2 note-off, 3 note-on
    events: list[tuple[int, int, int, mido.Message]] = []
    seq = 0

    def add(tick: int, prio: int, msg: mido.Message) -> None:
        nonlocal seq
        events.append((tick, prio, seq, msg))
        seq += 1

    for tempo in tempos:
        add(tempo.tick, 0, mido.MetaMessage("set_tempo", tempo=tempo.micros, time=0))
    for sig in sigs:
        add(
            sig.tick,
            0,
            mido.MetaMessage(
                "time_signature",
                numerator=sig.beats,
                denominator=sig.beat_type,
                clocks_per_click=24,
                notated_32nd_notes_per_beat=8,
                time=0,
            ),
        )
    for tick, fifths, minor in part.keys:
        # mido key_signature 接受 'C'..'Cb' 等调名字符串;fifths -> 调名
        major = ["Cb", "Gb", "Db", "Ab", "Eb", "Bb", "F", "C", "G", "D", "A", "E", "B", "F#", "C#"]
        minor_names = [
            "Abm",
            "Ebm",
            "Bbm",
            "Fm",
            "Cm",
            "Gm",
            "Dm",
            "Am",
            "Em",
            "Bm",
            "F#m",
            "C#m",
            "G#m",
            "D#m",
            "A#m",
        ]
        key_name = (minor_names if minor else major)[fifths + 7]
        add(tick, 0, mido.MetaMessage("key_signature", key=key_name, time=0))
    for pev in part.pizz_events:
        program = 45 if pev.pizz else part.program
        add(pev.tick, 1, mido.Message("program_change", channel=channel, program=program, time=0))
    for note in part.notes:
        add(
            note.start,
            3,
            mido.Message(
                "note_on", channel=channel, note=note.pitch, velocity=note.velocity, time=0
            ),
        )
        add(
            note.end,
            2,
            mido.Message("note_off", channel=channel, note=note.pitch, velocity=0, time=0),
        )
    events.sort(key=lambda e: (e[0], e[1], e[2]))
    previous = 0
    for tick, _prio, _seq, msg in events:
        msg.time = tick - previous
        previous = tick
        track.append(msg)
    track.append(mido.MetaMessage("end_of_track", time=0))
    return mid
