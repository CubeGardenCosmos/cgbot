"""plan.json 读写与源通道映射。

--font-for-channel / --reverb 按 MIDI 源通道(1-16)匹配声部;
引擎由声部 engine_hint 优先,否则用 --engine。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cgmusic.errors import BAD_INPUT, CgmusicError
from cgmusic.options import RenderOptions

HINT_ENGINES = ("muse", "fluidsynth")
MAX_SEND = 127


@dataclass(frozen=True)
class PartTask:
    id: str
    name: str
    file: str  # plan 目录内相对路径,如 parts/P1.mid
    engine: str
    soundfont: str | None
    channels: tuple[int, ...]


def read_plan(path: Path) -> dict[str, Any]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise CgmusicError(BAD_INPUT, f"无法读取 plan.json: {exc}") from exc
    parts = doc.get("parts") if isinstance(doc, dict) else None
    if not isinstance(parts, list) or not parts or not all(isinstance(p, dict) for p in parts):
        raise CgmusicError(BAD_INPUT, "plan.json 必须包含非空 parts 对象数组")
    ids = [str(part.get("id", "")) for part in parts]
    if not all(ids) or len(set(ids)) != len(ids):
        raise CgmusicError(BAD_INPUT, "plan.json 声部 id 缺失或重复")
    return doc


def save_plan(path: Path, doc: dict[str, Any]) -> None:
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def source_channels(part: dict[str, Any]) -> tuple[int, ...]:
    raw = part.get("source_channel")
    values = raw if isinstance(raw, list) else [raw]
    channels = [
        v for v in values if isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 16
    ]
    if not values or len(channels) != len(values):
        raise CgmusicError(BAD_INPUT, f"声部 {part.get('id')} 的 source_channel 无效")
    return tuple(sorted(channels))


def _single(values: set[Any], flag: str, part_id: str) -> Any:
    if len(values) > 1:
        raise CgmusicError(BAD_INPUT, f"声部 {part_id} 的源通道对 {flag} 的设定相互冲突")
    return next(iter(values)) if values else None


def resolve_parts(doc: dict[str, Any], opts: RenderOptions) -> list[PartTask]:
    """逐声部解析字体、引擎与混响发送;混响发送写回 doc 的 mix.reverb_send(0-1)供 cgmaster 使用。"""
    tasks: list[PartTask] = []
    for part in doc["parts"]:
        part_id = str(part["id"])
        file = part.get("file")
        if not isinstance(file, str) or not file:
            raise CgmusicError(BAD_INPUT, f"声部 {part_id} 缺少 file")
        channels = source_channels(part)
        font = _single({opts.font_for_channel[c] for c in channels if c in opts.font_for_channel},
                       "--font-for-channel", part_id)
        send = _single({opts.reverb[c] for c in channels if c in opts.reverb}, "--reverb", part_id)
        if send is not None:
            mix = part.get("mix")
            if not isinstance(mix, dict):
                mix = {}
                part["mix"] = mix
            mix["reverb_send"] = send / MAX_SEND
        hint = part.get("engine_hint")
        engine = hint if hint in HINT_ENGINES else opts.engine
        if font is not None and engine == "muse":
            raise CgmusicError(
                BAD_INPUT, f"声部 {part_id} 使用 muse 引擎,不能指定 --font-for-channel"
            )
        if font is not None:
            soundfont = font
        elif engine == "fluidsynth":
            soundfont = opts.soundfont
        else:
            soundfont = None
        tasks.append(
            PartTask(
                id=part_id,
                name=str(part.get("name") or part_id),
                file=file,
                engine=engine,
                soundfont=soundfont,
                channels=channels,
            )
        )
    return tasks
