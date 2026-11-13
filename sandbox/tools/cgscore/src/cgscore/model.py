"""共享数据模型:声部、速度表、plan.json 序列化。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import mido  # noqa: F401  (仅类型标注)

# 允许的种类枚举(共享契约)
FAMILIES = {
    "piano",
    "keys",
    "strings",
    "woodwinds",
    "brass",
    "percussion",
    "drums",
    "guitar",
    "bass",
    "voice",
    "synth",
    "other",
}


@dataclass
class Part:
    """单个声部:一个 MIDI 文件、一个乐器、一个通道。"""

    id: str
    name: str
    gm_program: int = 0
    is_drum: bool = False
    channel: int = 0  # 0 基;鼓固定 9
    muse_name: str = ""
    family: str = "other"
    engine_hint: str = "muse|fluidsynth|null"
    mid: mido.MidiFile | None = None  # type: ignore[name-defined]  # 由 prepare 填充
    duration_s: float = 0.0
    mix: dict = field(default_factory=dict)


def write_plan(parts: list[Part], source: str, outdir: Path) -> Path:
    duration = max((p.duration_s for p in parts), default=0.0)
    doc = {
        "version": 1,
        "source": source,
        "duration_s": round(duration, 6),
        "parts": [
            {
                "id": p.id,
                "name": p.name,
                "file": f"parts/{p.id}.mid",
                "family": p.family,
                "gm_program": p.gm_program,
                "is_drum": p.is_drum,
                "muse_name": p.muse_name,
                "engine_hint": p.engine_hint,
                "mix": p.mix,
            }
            for p in parts
        ],
    }
    path = outdir / "plan.json"
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
