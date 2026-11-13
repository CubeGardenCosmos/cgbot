"""测试用假 cgsynth:协议与真实二进制一致(render/soundfonts/presets),音频为确定性正弦波。

行为由环境变量控制:
- FAKE_CGSYNTH_LOG: 事件日志(JSON Lines),记录 start/fail/end 及时间戳。
- FAKE_CGSYNTH_SLEEP: 渲染前后的模拟耗时(秒)。
- FAKE_CGSYNTH_FAIL=<部件 id>: 该部件立即失败;错误码取 FAKE_CGSYNTH_FAIL_CODE(默认 synth_failed)。
- FAKE_CGSYNTH_MISSING=<部件 id>: fluidsynth 报告一个缺失音色。
- FAKE_CGSYNTH_FALLBACK=<部件 id>: muse 报告一个回退项。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import mido
import numpy as np
import soundfile as sf

RATE = 44100
TAIL_S = 4.0
SOUNDFONTS = {"GM": "GM.sf2", "Alt": "Alt.sf3"}


def log(event: str, **fields: object) -> None:
    path = os.environ.get("FAKE_CGSYNTH_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"event": event, "t": time.time(), **fields}) + "\n")


def fail(code: str, message: str) -> int:
    print(json.dumps({"error": code, "message": message}), file=sys.stderr)
    return 1


def render(args: list[str]) -> int:
    if not args:
        return fail("bad_input", "render needs a MIDI file")
    mid = Path(args[0])
    engine = soundfont = out = None
    rest = iter(args[1:])
    for arg in rest:
        value = next(rest, None)
        if value is None:
            return fail("bad_input", f"missing value for {arg}")
        if arg == "--engine":
            engine = value
        elif arg == "--soundfont":
            soundfont = value
        elif arg == "-o":
            out = Path(value)
        else:
            return fail("bad_input", f"unexpected argument {arg}")
    if engine not in ("fluidsynth", "muse") or out is None:
        return fail("bad_input", "render needs --engine and -o")
    part = out.stem
    resolved = None if engine == "muse" else (soundfont or "GM")
    log("start", part=part, engine=engine, soundfont=resolved, argv=args)
    if os.environ.get("FAKE_CGSYNTH_FAIL") == part:
        log("fail", part=part)
        code = os.environ.get("FAKE_CGSYNTH_FAIL_CODE", "synth_failed")
        return fail(code, f"fake failure for {part}")
    if engine == "muse" and soundfont is not None:
        return fail("bad_input", "--soundfont is not supported with muse")
    if resolved is not None and resolved not in SOUNDFONTS:
        return fail("bad_input", f"unknown soundfont {resolved}")
    time.sleep(float(os.environ.get("FAKE_CGSYNTH_SLEEP", "0") or 0))
    duration = round(mido.MidiFile(mid).length + TAIL_S, 6)
    tone = (0.2 * np.sin(2 * np.pi * 440 * np.arange(int(duration * RATE)) / RATE)).astype(
        np.float32
    )
    sf.write(str(out), np.stack([tone, tone], axis=1), RATE, subtype="FLOAT")
    if engine == "muse":
        fallbacks = (
            [{"from": "muse", "to": "fluidsynth", "reason": "fake"}]
            if os.environ.get("FAKE_CGSYNTH_FALLBACK") == part
            else []
        )
        report = {
            "engine": "muse",
            "duration_s": duration,
            "soundfont": None,
            "fonts": [],
            "missing_presets": [],
            "fallbacks": fallbacks,
            "muse": {"version": "fake-1", "dry_seconds": duration, "tracks": [{"name": part}]},
        }
    else:
        missing = (
            [{"program": 5, "name": "Fake"}]
            if os.environ.get("FAKE_CGSYNTH_MISSING") == part
            else []
        )
        report = {
            "engine": "fluidsynth",
            "duration_s": duration,
            "soundfont": resolved,
            "fonts": [{"id": resolved, "file": SOUNDFONTS[resolved], "missing_presets": missing}],
            "missing_presets": missing,
            "fallbacks": [],
            "muse": None,
        }
    log("end", part=part)
    print(json.dumps(report))
    return 0


def main(args: list[str]) -> int:
    if not args:
        return fail("unsupported", "no command")
    command, rest = args[0], args[1:]
    if command == "render":
        return render(rest)
    if command == "soundfonts":
        fonts = [
            {
                "id": "GM",
                "file": "GM.sf2",
                "bytes": 1,
                "default": True,
                "format": "sf2",
                "preset_count": 128,
            },
            {
                "id": "Alt",
                "file": "Alt.sf3",
                "bytes": 1,
                "default": False,
                "format": "sf3",
                "preset_count": 128,
            },
        ]
        print(json.dumps({"dir": "/fake/soundfonts", "fonts": fonts}))
        return 0
    if command == "presets" and len(rest) == 1:
        print(json.dumps({"soundfont": rest[0], "file": rest[0], "presets": []}))
        return 0
    return fail("unsupported", f"Unknown command: {command}")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
