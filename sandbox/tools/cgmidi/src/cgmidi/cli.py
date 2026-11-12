"""cgmidi 命令行。stdout 输出一行 JSON;错误以 {"error", "message"} 写入 stderr 并非零退出。

通道号在命令行上为 1-16(与乐谱软件一致),音色与音库为 0-127。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

from cgmidi import ops
from cgmidi.errors import BAD_INPUT, CgmidiError
from cgmidi.files import load_midi, save_midi

Converter = Callable[[str, str], int]


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        _emit_error(BAD_INPUT, message)
        raise SystemExit(2)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="cgmidi",
        description="MIDI 预处理:速度、移调、通道/音色/音库、静音独奏、裁剪",
    )
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)

    info = sub.add_parser("info", help="读取 MIDI 摘要(速度、时长、通道、音色、音库、音域)")
    info.add_argument("input", help="输入 .mid 文件")

    proc = sub.add_parser("process", help="预处理并写出新的 MIDI 文件")
    proc.add_argument("input", help="输入 .mid 文件(只读,不会被修改)")
    proc.add_argument("-o", "--output", required=True, help="输出 .mid 文件路径")
    tempo = proc.add_mutually_exclusive_group()
    tempo.add_argument(
        "--speed",
        type=float,
        help="速度倍率(0.1-10),1.25 为加快 25%%,0.5 为减半",
    )
    tempo.add_argument("--bpm", type=float, help="替换全部速度标记为固定 BPM(1-999)")
    proc.add_argument(
        "--transpose",
        type=int,
        default=0,
        help="移调半音数(-48 到 48),鼓组通道 10 不移调",
    )
    proc.add_argument(
        "--mute",
        action="append",
        default=[],
        metavar="CH",
        help="静音通道 1-16,可重复",
    )
    proc.add_argument(
        "--solo",
        action="append",
        default=[],
        metavar="CH",
        help="独奏通道 1-16,可重复;指定后只保留这些通道的音符",
    )
    proc.add_argument(
        "--map-channel",
        action="append",
        default=[],
        metavar="SRC:DST",
        help="通道改派(1-16),如 2:3,可重复",
    )
    proc.add_argument(
        "--set-program",
        action="append",
        default=[],
        metavar="CH=PROG",
        help="强制通道音色(0-127),删除该通道原有音色切换后在开头设定",
    )
    proc.add_argument(
        "--set-bank",
        action="append",
        default=[],
        metavar="CH=MSB[:LSB]",
        help="强制通道音库(CC0/CC32),LSB 默认 0",
    )
    proc.add_argument(
        "--map-program",
        action="append",
        default=[],
        metavar="SRC=DST",
        help="音色号(0-127)映射,作用于所有通道,可重复",
    )
    proc.add_argument("--start", type=float, help="裁剪起点(秒,按原乐谱时间)")
    proc.add_argument("--end", type=float, help="裁剪终点(秒,按原乐谱时间)")
    return parser


def _number(text: str, flag: str, low: int, high: int, what: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise CgmidiError(BAD_INPUT, f"{flag} 需要整数,收到 {text!r}") from None
    if not low <= value <= high:
        raise CgmidiError(
            BAD_INPUT,
            f"{flag} 的{what}须在 {low}-{high} 之间,收到 {value}",
        )
    return value


def _channel(text: str, flag: str) -> int:
    """命令行通道 1-16 转为 mido 通道 0-15。"""
    return _number(text, flag, 1, 16, "通道") - 1


def _data(text: str, flag: str) -> int:
    return _number(text, flag, 0, 127, "音色/音库值")


def _mapping(
    items: list[str],
    sep: str,
    flag: str,
    left: Converter,
    right: Converter,
) -> dict[int, int]:
    result: dict[int, int] = {}
    for item in items:
        left_text, found, right_text = item.partition(sep)
        if not (found and left_text and right_text):
            raise CgmidiError(BAD_INPUT, f"{flag} 格式应为 A{sep}B,收到 {item!r}")
        key = left(left_text, flag)
        if key in result:
            raise CgmidiError(BAD_INPUT, f"{flag} 对同一源值重复指定: {left_text!r}")
        result[key] = right(right_text, flag)
    return result


def _banks(items: list[str]) -> dict[int, tuple[int, int]]:
    result: dict[int, tuple[int, int]] = {}
    for item in items:
        channel_text, found, value = item.partition("=")
        msb_text, _, lsb_text = value.partition(":")
        if not (found and channel_text and msb_text):
            raise CgmidiError(BAD_INPUT, f"--set-bank 格式应为 CH=MSB[:LSB],收到 {item!r}")
        channel = _channel(channel_text, "--set-bank")
        if channel in result:
            raise CgmidiError(BAD_INPUT, f"--set-bank 对同一通道重复指定: {channel_text!r}")
        lsb = _data(lsb_text, "--set-bank") if lsb_text else 0
        result[channel] = (_data(msb_text, "--set-bank"), lsb)
    return result


def _build_options(args: argparse.Namespace) -> ops.Options:
    return ops.Options(
        speed=args.speed,
        bpm=args.bpm,
        transpose=args.transpose,
        mute={_channel(text, "--mute") for text in args.mute},
        solo={_channel(text, "--solo") for text in args.solo},
        map_channel=_mapping(args.map_channel, ":", "--map-channel", _channel, _channel),
        set_program=_mapping(args.set_program, "=", "--set-program", _channel, _data),
        set_bank=_banks(args.set_bank),
        map_program=_mapping(args.map_program, "=", "--map-program", _data, _data),
        start=args.start,
        end=args.end,
    )


def run(args: argparse.Namespace) -> dict[str, object]:
    if args.command == "info":
        return ops.describe(load_midi(args.input))

    if Path(args.output).resolve() == Path(args.input).resolve():
        raise CgmidiError(BAD_INPUT, "输出路径不能与输入文件相同")
    opts = _build_options(args)
    result = ops.transform(load_midi(args.input), opts)
    output = Path(args.output)
    save_midi(result, output)
    summary = ops.describe(result)
    return {
        "output": str(output),
        "duration_seconds": summary["duration_seconds"],
        "notes": summary["notes"],
    }


def _emit_error(code: str, message: str) -> None:
    print(json.dumps({"error": code, "message": message}, ensure_ascii=False), file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run(args)
    except CgmidiError as exc:
        _emit_error(exc.code, exc.message)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0
