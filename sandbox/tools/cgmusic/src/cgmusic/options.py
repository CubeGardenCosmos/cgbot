"""cgmusic render 参数解析与数值校验。范围、重复项与 muse 互斥规则沿用旧 C++ CLI。"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

from cgmusic.errors import BAD_INPUT, CgmusicError

FORMATS = ("mp3", "wav", "flac")
ENGINES = ("fluidsynth", "muse")
MAX_GAIN_DB = 12.0
MAX_JOBS = 64
MAX_CHANNEL = 16
USAGE = (
    "usage: cgmusic render <file> [--engine fluidsynth|muse] [--soundfont ID|FILE] "
    "[--fmt mp3|wav|flac] [-o OUT] [--gain G] [--reverb CH=0-127]... "
    "[--font-for-channel CH=ID]... [--reverb-room R] [--reverb-level L] [--jobs N]"
)
_VALUE_FLAGS = frozenset(
    {
        "--engine",
        "--soundfont",
        "--fmt",
        "-o",
        "--output",
        "--gain",
        "--reverb",
        "--font-for-channel",
        "--reverb-room",
        "--reverb-level",
        "--jobs",
    }
)


@dataclass
class RenderOptions:
    input: Path
    engine: str = "fluidsynth"
    soundfont: str | None = None
    fmt: str = "mp3"
    output: Path | None = None
    gain: float = 0.0
    reverb: dict[int, int] = field(default_factory=dict)  # 源通道 1-16 -> 混响发送 0-127
    font_for_channel: dict[int, str] = field(default_factory=dict)  # 源通道 1-16 -> 字体 ID/文件
    reverb_room: float = 0.75
    reverb_level: float = 1.0
    jobs: int | None = None


def _number(text: str | None, low: float, high: float, name: str, default: float) -> float:
    if text is None:
        return default
    try:
        value = float(text)
    except ValueError:
        raise CgmusicError(BAD_INPUT, f"Invalid {name}") from None
    if not math.isfinite(value) or not low <= value <= high:
        raise CgmusicError(BAD_INPUT, f"{name} is out of range")
    return value


def _integer(text: str, low: int, high: int, name: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise CgmusicError(BAD_INPUT, f"{name} must be an integer") from None
    if not low <= value <= high:
        raise CgmusicError(BAD_INPUT, f"{name} is out of range")
    return value


def _channel_pair(text: str, flag: str) -> tuple[int, str]:
    channel_text, sep, value = text.partition("=")
    if not sep or not value:
        raise CgmusicError(BAD_INPUT, f"{flag} expects CH=VALUE")
    return _integer(channel_text, 1, MAX_CHANNEL, f"{flag} channel"), value


def parse_render_args(argv: list[str]) -> RenderOptions:
    pairs: list[tuple[str, str]] = []
    positional: list[str] = []
    items = iter(argv)
    for arg in items:
        if arg in _VALUE_FLAGS:
            value = next(items, None)
            if value is None:
                raise CgmusicError(BAD_INPUT, f"Missing value for {arg}")
            pairs.append((arg, value))
        elif arg.startswith("-"):
            raise CgmusicError(BAD_INPUT, f"Unknown option {arg}")
        else:
            positional.append(arg)
    if not positional:
        raise CgmusicError(BAD_INPUT, USAGE)
    if len(positional) > 1:
        raise CgmusicError(BAD_INPUT, "Only one input file is accepted")

    def last(*flags: str) -> str | None:
        found = [value for flag, value in pairs if flag in flags]
        return found[-1] if found else None

    def every(flag: str) -> list[str]:
        return [value for key, value in pairs if key == flag]

    engine = last("--engine")
    engine = "fluidsynth" if engine is None else engine
    if engine not in ENGINES:
        raise CgmusicError(BAD_INPUT, f"Unknown engine: {engine}")
    fmt_text = last("--fmt")
    fmt = "mp3" if fmt_text is None else fmt_text.lower()
    if fmt not in FORMATS:
        raise CgmusicError(BAD_INPUT, f"Unknown format: {fmt}")
    soundfont = last("--soundfont")
    if soundfont == "":
        raise CgmusicError(BAD_INPUT, "--soundfont must not be empty")
    output_text = last("-o", "--output")
    if output_text == "":
        raise CgmusicError(BAD_INPUT, "-o must not be empty")

    reverb: dict[int, int] = {}
    for text in every("--reverb"):
        channel, send_text = _channel_pair(text, "--reverb")
        if channel in reverb:
            raise CgmusicError(BAD_INPUT, f"Duplicate --reverb for channel {channel}")
        reverb[channel] = _integer(send_text, 0, 127, "--reverb send")

    fonts: dict[int, str] = {}
    for text in every("--font-for-channel"):
        channel, spec = _channel_pair(text, "--font-for-channel")
        if channel in fonts:
            raise CgmusicError(BAD_INPUT, f"Duplicate --font-for-channel for channel {channel}")
        fonts[channel] = spec

    if engine == "muse" and (soundfont is not None or fonts):
        raise CgmusicError(BAD_INPUT, "--soundfont 与 --font-for-channel 仅适用于 fluidsynth 引擎")

    jobs_text = last("--jobs")
    jobs = None if jobs_text is None else _integer(jobs_text, 1, MAX_JOBS, "--jobs")
    return RenderOptions(
        input=Path(positional[0]),
        engine=engine,
        soundfont=soundfont,
        fmt=fmt,
        output=None if output_text is None else Path(output_text),
        gain=_number(last("--gain"), -MAX_GAIN_DB, MAX_GAIN_DB, "--gain", 0.0),
        reverb=reverb,
        font_for_channel=fonts,
        reverb_room=_number(last("--reverb-room"), 0.0, 1.0, "--reverb-room", 0.75),
        reverb_level=_number(last("--reverb-level"), 0.0, 1.0, "--reverb-level", 1.0),
        jobs=jobs,
    )
