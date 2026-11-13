"""命令行入口：成功 stdout 单行 JSON，失败 stderr JSON 并非零退出。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import NoReturn

from cgmaster.errors import BAD_INPUT, CgmasterError
from cgmaster.master import master


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        _emit_error(BAD_INPUT, message)
        raise SystemExit(2)


def build_parser() -> _Parser:
    parser = _Parser(prog="cgmaster", description="干声分轨混音、母带与编码")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    mix = commands.add_parser("mix", help="按 plan.json 混合分轨并输出母带")
    mix.add_argument("plan", help="前处理生成的 plan.json")
    mix.add_argument("--stems", required=True, help="包含 <声部id>.wav 的目录")
    mix.add_argument("--fmt", choices=["mp3", "wav", "flac"], default="mp3")
    mix.add_argument("-o", "--output", help="输出路径，默认为 plan.json 所在目录/master.<fmt>")
    mix.add_argument("--gain", type=float, default=0.0, help="相对 -14 LUFS 的偏移，截到 ±12 dB")
    mix.add_argument("--reverb-room", type=float, default=0.75, help="混响房间大小 0-1")
    mix.add_argument("--reverb-level", type=float, default=1.0, help="混响总线电平 0-1.5")
    return parser


def _emit_error(code: str, message: str) -> None:
    print(json.dumps({"error": code, "message": message}, ensure_ascii=False), file=sys.stderr)


def main() -> None:
    args = build_parser().parse_args()
    plan = Path(args.plan)
    output = Path(args.output) if args.output else plan.parent / f"master.{args.fmt}"
    try:
        report = master(
            plan,
            args.stems,
            output,
            fmt=args.fmt,
            gain=args.gain,
            reverb_room=args.reverb_room,
            reverb_level=args.reverb_level,
        )
    except CgmasterError as exc:
        _emit_error(exc.code, exc.message)
        raise SystemExit(1) from None
    print(json.dumps(report, ensure_ascii=False, allow_nan=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
