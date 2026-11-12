"""MIDI 文件读写。读取时校验存在性与格式;写出采用临时文件 + fsync + 原子替换。"""

from __future__ import annotations

import os
import struct
from pathlib import Path

from mido import MidiFile

from cgmidi.errors import BAD_INPUT, PARSE_FAILED, UNSUPPORTED_FORMAT, WRITE_FAILED, CgmidiError


def load_midi(path: str) -> MidiFile:
    source = Path(path)
    if not source.is_file():
        raise CgmidiError(BAD_INPUT, f"输入文件不存在: {path}")
    try:
        mid = MidiFile(str(source))
    except (OSError, EOFError, ValueError, KeyError, IndexError, struct.error) as exc:
        raise CgmidiError(PARSE_FAILED, f"无法解析 MIDI 文件: {exc}") from exc
    if mid.type == 2:
        raise CgmidiError(UNSUPPORTED_FORMAT, "不支持 MIDI 格式 2(异步多轨)")
    return mid


def save_midi(mid: MidiFile, target: Path) -> None:
    if not target.parent.is_dir():
        raise CgmidiError(BAD_INPUT, f"输出目录不存在: {target.parent}")
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        mid.save(str(tmp))
        with open(tmp, "rb+") as handle:
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except (OSError, ValueError) as exc:
        raise CgmidiError(WRITE_FAILED, f"写入 MIDI 失败: {exc}") from exc
    finally:
        tmp.unlink(missing_ok=True)
