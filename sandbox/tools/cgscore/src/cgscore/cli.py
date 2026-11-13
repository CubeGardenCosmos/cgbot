"""cgscore 命令行入口。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .musicxml import ScoreError
from .prepare import prepare


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cgscore", description="乐谱前处理:拆分声部 MIDI + plan.json"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser(
        "prepare", help="输入 .mid/.midi/.musicxml/.xml/.mxl,输出声部文件与 plan.json"
    )
    prep.add_argument("input", type=Path)
    prep.add_argument("-o", "--output", type=Path, required=True, help="输出目录")
    args = parser.parse_args(argv)

    if args.command == "prepare":
        try:
            plan = prepare(args.input, args.output)
        except (ScoreError, ValueError, OSError) as exc:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
            return 1
        doc = json.loads(plan.read_text(encoding="utf-8"))
        print(
            json.dumps(
                {"plan": str(plan), "parts": len(doc["parts"]), "duration_s": doc["duration_s"]},
                ensure_ascii=False,
            )
        )
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
