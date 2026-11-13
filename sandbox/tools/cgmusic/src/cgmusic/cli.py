"""cgmusic 命令行入口:成功时 stdout 单行 JSON;失败时 stderr 单行 JSON 并以非零退出。

不提供 --help:与旧 C++ CLI 一致,未知命令返回 unsupported。
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

from cgmusic import synth
from cgmusic.analyze import analyze
from cgmusic.errors import BAD_INPUT, SYNTH_FAILED, UNSUPPORTED, CgmusicError
from cgmusic.options import parse_render_args
from cgmusic.pipeline import render

USAGE = "usage: cgmusic render|presets|soundfonts|analyze ..."


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        report = _dispatch(args)
    except CgmusicError as exc:
        print(exc.to_json(), file=sys.stderr)
        return 1
    except Exception as exc:  # 与旧 CLI 一致:未预期异常统一为 synth_failed
        print(CgmusicError(SYNTH_FAILED, str(exc)).to_json(), file=sys.stderr)
        return 1
    print(json.dumps(_finite(report), ensure_ascii=False, separators=(",", ":"), allow_nan=False))
    return 0


def _dispatch(argv: list[str]) -> dict[str, Any]:
    if not argv:
        raise CgmusicError(BAD_INPUT, USAGE)
    command, rest = argv[0], argv[1:]
    if command == "render":
        return render(parse_render_args(rest))
    if command == "presets":
        return _presets(rest)
    if command == "soundfonts":
        if rest:
            raise CgmusicError(BAD_INPUT, "usage: cgmusic soundfonts")
        return synth.run_json(synth.locate(), ["soundfonts"])
    if command == "analyze":
        if len(rest) != 1:
            raise CgmusicError(BAD_INPUT, "usage: cgmusic analyze <audio>")
        return analyze(Path(rest[0]))
    raise CgmusicError(UNSUPPORTED, f"Unknown command: {command}")


def _presets(rest: list[str]) -> dict[str, Any]:
    spec: str | None = None
    if rest:
        if len(rest) != 2 or rest[0] != "--soundfont":
            raise CgmusicError(BAD_INPUT, "usage: cgmusic presets [--soundfont ID|FILE]")
        spec = rest[1]
    binary = synth.locate()
    if spec is None:
        fonts = synth.run_json(binary, ["soundfonts"]).get("fonts", [])
        defaults = [font for font in fonts if font.get("default") is True]
        if not defaults:
            raise CgmusicError(BAD_INPUT, "没有默认字体")
        spec = str(defaults[0]["id"])
    return synth.run_json(binary, ["presets", spec])


def _finite(value: Any) -> Any:
    """非有限浮点输出为 null,保证 stdout 始终是合法 JSON。"""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _finite(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_finite(item) for item in value]
    return value
