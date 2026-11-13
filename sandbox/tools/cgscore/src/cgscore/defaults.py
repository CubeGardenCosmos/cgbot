"""plan.json 默认值:移植自 C++ 核心的 balance/sends/muse mapping 表。

来源(行为基准,不新增启发式):
- sandbox/tools/cgmusic/src/balance.cpp / balance.hpp(家族电平,kBalanceBaseDb=-6)
- sandbox/tools/cgmusic/src/sends.cpp(CC91/CC93 默认发送量)
- sandbox/tools/cgmusic/src/muse/mapping.cpp(GM 程序/声部名 -> Muse 乐器名)
- sandbox/tools/cgmusic/src/muse/render.cpp(Muse 打击乐与 FluidSynth 回退的 -12 dB)
"""

from __future__ import annotations

BALANCE_BASE_DB = -6.0

# (首程序, 末程序, 家族, 偏移 dB);未命中为 Melodic(偏移 0)
_PROGRAM_RULES = [
    (21, 21, "accordion", -6.0),
    (23, 23, "accordion", -6.0),
    (56, 63, "brass", 6.0),
    (64, 79, "woodwind", -6.0),
]

# GM 程序族(0 基闭区间)-> plan.family;仅用于 family 字段,电平仍由 balance 规则决定
_FAMILY_RANGES = [
    (0, 7, "piano"),
    (8, 15, "percussion"),
    (16, 23, "keys"),
    (24, 31, "guitar"),
    (32, 39, "bass"),
    (40, 47, "strings"),
    (48, 51, "strings"),
    (52, 54, "voice"),
    (55, 55, "percussion"),
    (56, 63, "brass"),
    (64, 79, "woodwinds"),
    (80, 87, "synth"),
    (88, 95, "synth"),
    (96, 103, "synth"),
    (104, 111, "other"),
    (112, 119, "percussion"),
    (120, 127, "other"),
]

# CC91/CC93 默认发送量(sends.cpp kProgramSends 原样)
_SEND_RANGES = [
    (0, 7, 30, 0),
    (8, 15, 25, 0),
    (16, 23, 35, 0),
    (24, 31, 25, 0),
    (32, 39, 10, 0),
    (40, 47, 55, 0),
    (48, 51, 60, 0),
    (52, 54, 65, 0),
    (55, 55, 60, 0),
    (56, 63, 40, 0),
    (64, 71, 35, 0),
    (72, 79, 35, 0),
    (80, 87, 30, 0),
    (88, 95, 65, 15),
    (96, 103, 60, 0),
    (104, 111, 35, 0),
    (112, 119, 20, 0),
    (120, 127, 40, 0),
]
_DRUM_SEND = 15

# Muse 候选表:(drum, first, last, name 或 None=保留给 FluidSynth)
# abbr 回退不需要:plan 只记录目录精确名,核心按名匹配目录。
_CANDIDATES = [
    (True, 0, 127, "Drum Kit"),
    (True, 0, 127, "GM Kit"),
    (False, 0, 2, "Piano"),
    (False, 3, 3, "Upright Piano"),
    (False, 3, 3, "Piano"),
    (False, 4, 5, None),
    (False, 6, 6, "Harpsichord"),
    (False, 8, 8, "Celesta"),
    (False, 9, 9, "Glockenspiel"),
    (False, 10, 10, "Jewellery Music Box"),
    (False, 10, 10, "Joana's Music Box"),
    (False, 11, 11, "Vibraphone"),
    (False, 12, 12, "Marimba"),
    (False, 13, 13, "Xylophone"),
    (False, 14, 14, "Tubular Bells"),
    (False, 16, 18, "Hammond Organ"),
    (False, 24, 24, "Acoustic Nylon"),
    (False, 25, 25, "Acoustic Steel Picked"),
    (False, 25, 25, "Acoustic Steel Plucked"),
    (False, 26, 26, "Electric SC - Clean"),
    (False, 27, 28, "Electric LP - Clean"),
    (False, 29, 30, "Electric LP - Heavy"),
    (False, 31, 31, "Electric SC - Clean"),
    (False, 33, 36, "Electric Bass"),
    (False, 40, 40, "Violin 1 Solo"),
    (False, 40, 40, "Violins 1"),
    (False, 41, 41, "Viola Solo"),
    (False, 41, 41, "Violas"),
    (False, 42, 42, "Cello Solo"),
    (False, 42, 42, "Cellos"),
    (False, 43, 43, "Contrabasses"),
    (False, 44, 45, "Violins 1"),
    (False, 46, 46, "Harp"),
    (False, 47, 47, "Timpani"),
    (False, 48, 48, "Violins 1"),
    (False, 52, 54, "Full Choir"),
    (False, 56, 56, "Trumpet"),
    (False, 57, 57, "Trombone"),
    (False, 58, 58, "Tuba"),
    (False, 59, 59, "Trumpet"),
    (False, 60, 60, "French Horn"),
    (False, 61, 61, "Trumpets a4"),
    (False, 64, 64, "Soprano Saxophone"),
    (False, 65, 65, "Alto Saxophone"),
    (False, 66, 66, "Tenor Saxophone"),
    (False, 67, 67, "Baritone Saxophone"),
    (False, 68, 68, "Oboe"),
    (False, 69, 69, "English Horn"),
    (False, 70, 70, "Bassoon"),
    (False, 71, 71, "Clarinet in Bb"),
    (False, 72, 72, "Piccolo"),
    (False, 73, 73, "Flute 1"),
    (False, 113, 113, "Agogos"),
    (False, 115, 115, "Wood Blocks"),
    (False, 116, 116, "Taikos"),
    (False, 117, 117, "Toms"),
]


def family_for(program: int, is_drum: bool) -> str:
    if is_drum:
        return "drums"
    for first, last, family in _FAMILY_RANGES:
        if first <= program <= last:
            return family
    return "other"


def balance_gain_db(program: int, is_drum: bool) -> float:
    """balance.cpp 的 stem_gain_db(balance=on),鼓组取 OtherDrum 绝对增益。"""
    if is_drum:
        return -6.0  # OtherDrum;Kick +6 / Snare 0 为单音规则,声部级默认取 OtherDrum
    for first, last, _family, offset in _PROGRAM_RULES:
        if first <= program <= last:
            return BALANCE_BASE_DB + offset
    return BALANCE_BASE_DB


def default_send(program: int, is_drum: bool) -> int:
    if is_drum:
        return _DRUM_SEND
    for first, last, reverb, _chorus in _SEND_RANGES:
        if first <= program <= last:
            return reverb
    return 0


def _pick_by_program(program: int, is_drum: bool) -> str:
    for drum, first, last, name in _CANDIDATES:
        if drum != is_drum:
            continue
        if is_drum or first <= program <= last:
            return name or ""  # None 行 = 保留 FluidSynth
    return ""


def muse_name_for(part_name: str, program: int, is_drum: bool, occurrence: int = 0) -> str:
    """mapping.cpp pick_instrument_by_name 的目录名版:返回精确 Muse 乐器名,'' 表示 FluidSynth。"""
    if part_name:
        s = part_name.lower()
        if "drum" in s or "percussion" in s:
            return "Drum Kit"
        if "accordion" in s:
            return ""  # 强制回退 FluidSynth
        if "flute" in s or "flauto" in s:
            prefer2 = "2" in s or (occurrence >= 1 and "1" not in s)
            return "Flute 2" if prefer2 else "Flute 1"
        if "bass clarinet" in s:
            return "Bass Clarinet"
        if "clarinet" in s:
            return "Clarinet in Bb"
        if "bassoon" in s:
            return "Bassoon"
        if "french horn" in s or "horn" in s:
            return "French Horn"
        if "trumpet" in s:
            return "Trumpet"
        if "contrabass" in s or "double bass" in s or s in ("basses", "bass"):
            return "Contrabasses"
        if "violoncello" in s or "cello" in s:
            return "Cellos"
        if "viola" in s:
            return "Violas"
        if "violin" in s:
            prefer2 = "2" in s or (occurrence >= 1 and "1" not in s)
            return "Violins 2" if prefer2 else "Violins 1"
        if "piano" in s:
            return "Piano"
    return _pick_by_program(program, is_drum)


def default_mix(part_name: str, program: int, is_drum: bool) -> tuple[str, dict]:
    """返回 (muse_name, mix 默认值)。增益已折入家族平衡与 -12 dB 引擎规则。"""
    muse_name = muse_name_for(part_name, program, is_drum)
    gain = balance_gain_db(program, is_drum)
    # muse/render.cpp:FluidSynth 回退与 Muse 打击乐家族先削 12 dB
    percussion = is_drum or (muse_name and ("Drum" in muse_name or "Perc" in muse_name))
    if not muse_name or percussion:
        gain -= 12.0
    mix = {
        "gain_db": round(gain, 6),
        "pan": 0.0,
        "reverb_send": round(default_send(program, is_drum) / 127.0, 6),
        "echo_send": 0.0,
    }
    return muse_name, mix
