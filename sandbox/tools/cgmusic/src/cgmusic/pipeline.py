"""render 主流程:cgscore 前处理 -> 并发 cgsynth 分轨渲染 -> cgmaster 母带,汇总为单个 JSON 报告。"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path
from typing import Any

import mido
from cgmaster import master
from cgmaster.errors import CgmasterError
from cgscore.prepare import prepare as prepare_score

from cgmusic import synth
from cgmusic.errors import (
    BAD_INPUT,
    MUSE_UNAVAILABLE,
    TOO_LONG,
    UNSUPPORTED,
    WRITE_FAILED,
    CgmusicError,
)
from cgmusic.options import RenderOptions
from cgmusic.plan import PartTask, read_plan, resolve_parts, save_plan

INPUT_SUFFIXES = frozenset({".mid", ".midi", ".musicxml", ".xml", ".mxl"})
MIDI_SUFFIXES = frozenset({".mid", ".midi"})
MAX_INPUT_BYTES = 32 * 1024 * 1024
MAX_MIDI_BYTES = 8 * 1024 * 1024
MAX_DURATION_S = 15 * 60
DEFAULT_OUTPUT_DIR = Path("/workspace/artifacts/music")
MUSE_WORKERS = 4
FLUIDSYNTH_WORKERS = 8


def render(opts: RenderOptions) -> dict[str, Any]:
    started = time.monotonic()
    source = _check_input(opts.input)
    output = (opts.output or DEFAULT_OUTPUT_DIR / f"{source.stem}.{opts.fmt}").absolute()
    binary = synth.locate()
    _ensure_output_dir(output.parent)
    with tempfile.TemporaryDirectory(
        prefix="cgmusic-", dir=_work_base(output), ignore_cleanup_errors=True
    ) as tmp:
        work = Path(tmp)
        plan_path = _prepare(source, work / "score")
        plan_dir = plan_path.parent
        doc = read_plan(plan_path)
        _check_duration(doc)
        doc["parts"] = _sounding_parts(plan_dir, doc["parts"])
        tasks = resolve_parts(doc, opts)
        _check_part_sizes(plan_dir, tasks)
        save_plan(plan_path, doc)
        stems = work / "stems"
        stems.mkdir()
        calls = [
            synth.RenderCall(
                key=task.id,
                argv=_render_argv(binary, plan_dir / task.file, task, stems / f"{task.id}.wav"),
                fallback_argv=_fallback_argv(
                    binary, plan_dir / task.file, task, stems / f"{task.id}.wav", opts
                )
                if task.engine == "muse"
                else None,
            )
            for task in tasks
        ]
        results = synth.render_all(calls, worker_count(opts, tasks))
        muse_tasks = [task for task in tasks if task.engine == "muse"]
        if muse_tasks and not any(results[task.id].fallback_reason is None for task in muse_tasks):
            raise CgmusicError(MUSE_UNAVAILABLE, "所有 Muse 声部均不可用并已回退 FluidSynth")
        mix = _mix(plan_path, stems, output, opts)
    return _report(mix, tasks, results, time.monotonic() - started)


def worker_count(opts: RenderOptions, tasks: list[PartTask]) -> int:
    if opts.jobs is not None:
        return min(opts.jobs, len(tasks))
    cap = MUSE_WORKERS if any(task.engine == "muse" for task in tasks) else FLUIDSYNTH_WORKERS
    return max(1, min(os.cpu_count() or 1, cap, len(tasks)))


def _check_input(path: Path) -> Path:
    suffix = path.suffix.lower()
    if suffix not in INPUT_SUFFIXES:
        raise CgmusicError(
            UNSUPPORTED,
            f"unsupported input type: {suffix or '(none)'} (expected {sorted(INPUT_SUFFIXES)})",
        )
    if not path.is_file():
        raise CgmusicError(BAD_INPUT, f"输入文件不存在: {path}")
    size = path.stat().st_size
    if size > MAX_INPUT_BYTES:
        raise CgmusicError(TOO_LONG, f"输入文件 {size} 字节超过上限 {MAX_INPUT_BYTES} 字节")
    if suffix in MIDI_SUFFIXES and size > MAX_MIDI_BYTES:
        raise CgmusicError(TOO_LONG, f"MIDI 文件 {size} 字节超过上限 {MAX_MIDI_BYTES} 字节")
    return path.absolute()


def _work_base(output: Path) -> Path:
    """临时工作目录位置:优先 TMPDIR,否则放在输出目录的同级目录。"""
    env = os.environ.get("TMPDIR")
    if env and Path(env).is_dir():
        return Path(env)
    return output.parent.parent


def _ensure_output_dir(directory: Path) -> None:
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CgmusicError(WRITE_FAILED, f"无法创建输出目录 {directory}: {exc}") from exc


def _prepare(source: Path, outdir: Path) -> Path:
    try:
        return prepare_score(source, outdir)
    except Exception as exc:  # cgscore 对 mido/musicxml 解析错误类型不统一,统一映射为坏输入
        raise CgmusicError(BAD_INPUT, f"乐谱前处理失败: {exc}") from exc


def _check_duration(doc: dict[str, Any]) -> None:
    duration = doc.get("duration_s", 0.0)
    if not isinstance(duration, int | float) or not 0.0 <= duration < float("inf"):
        raise CgmusicError(BAD_INPUT, "plan.json duration_s 无效")
    if duration > MAX_DURATION_S:
        raise CgmusicError(TOO_LONG, f"乐曲时长 {duration:.1f} s 超过上限 {MAX_DURATION_S} s")


def _sounding_parts(plan_dir: Path, parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """丢弃没有任何音符的声部:cgsynth 对空 MIDI 报错,旧 C++ 版本对空通道输出静音。"""
    sounding = [p for p in parts if _has_notes(plan_dir / p["file"])]
    if not sounding:
        raise CgmusicError(BAD_INPUT, "乐谱中没有任何音符")
    return sounding


def _has_notes(mid: Path) -> bool:
    return any(msg.type == "note_on" and msg.velocity > 0 for msg in mido.MidiFile(str(mid)))


def _check_part_sizes(plan_dir: Path, tasks: list[PartTask]) -> None:
    for task in tasks:
        mid = plan_dir / task.file
        try:
            size = mid.stat().st_size
        except OSError as exc:
            raise CgmusicError(BAD_INPUT, f"声部 MIDI 不存在: {mid}") from exc
        if size > MAX_MIDI_BYTES:
            raise CgmusicError(TOO_LONG, f"声部 {task.id} 的 MIDI {size} 字节超过上限")


def _render_argv(binary: str, mid: Path, task: PartTask, stem: Path) -> list[str]:
    argv = [binary, "render", str(mid), "--engine", task.engine]
    if task.soundfont is not None:
        argv += ["--soundfont", task.soundfont]
    argv += ["-o", str(stem)]
    return argv


def _fallback_argv(
    binary: str, mid: Path, task: PartTask, stem: Path, opts: RenderOptions
) -> list[str]:
    argv = [binary, "render", str(mid), "--engine", "fluidsynth"]
    if opts.soundfont is not None:
        argv += ["--soundfont", opts.soundfont]
    argv += ["-o", str(stem)]
    return argv


def _mix(plan_path: Path, stems: Path, output: Path, opts: RenderOptions) -> dict[str, Any]:
    try:
        return master(
            plan_path,
            stems,
            output,
            fmt=opts.fmt,
            gain=opts.gain,
            reverb_room=opts.reverb_room,
            reverb_level=opts.reverb_level,
        )
    except CgmasterError as exc:
        raise CgmusicError(exc.code, exc.message) from exc


def _report(
    mix: dict[str, Any],
    tasks: list[PartTask],
    results: dict[str, synth.RenderResult],
    wall_s: float,
) -> dict[str, Any]:
    fonts: dict[tuple[str, str], dict[str, Any]] = {}
    missing: list[dict[str, Any]] = []
    fallbacks: list[dict[str, Any]] = []
    muse_tracks: list[dict[str, Any]] = []
    muse_version: str | None = None
    dry_seconds = 0.0
    has_muse = False
    parts: list[dict[str, Any]] = []
    for task in tasks:
        result = results[task.id]
        rep = result.report
        parts.append(
            {
                "id": task.id,
                "name": task.name,
                "engine": rep.get("engine", task.engine),
                "wall_s": result.wall_s,
            }
        )
        for font in rep.get("fonts", []):
            key = (str(font.get("id")), str(font.get("file")))
            entry = fonts.setdefault(
                key, {"id": key[0], "file": key[1], "parts": [], "missing_presets": []}
            )
            entry["parts"].append(task.id)
            entry["missing_presets"].extend(
                {**m, "part": task.id} for m in font.get("missing_presets", [])
            )
        missing.extend({**m, "part": task.id} for m in rep.get("missing_presets", []))
        fallbacks.extend({**m, "part": task.id} for m in rep.get("fallbacks", []))
        if result.fallback_reason is not None:
            fallbacks.append(
                {
                    "channel": task.source_channel or (task.channels[0] if task.channels else 0),
                    "part": task.id,
                    "program": task.program,
                    "reason": result.fallback_reason,
                }
            )
        muse = rep.get("muse")
        if isinstance(muse, dict):
            has_muse = True
            muse_version = muse_version or str(muse.get("version", ""))
            dry_seconds = max(dry_seconds, float(muse.get("dry_seconds", 0.0)))
            muse_tracks.extend({**t, "part": task.id} for t in muse.get("tracks", []))
    return {
        "path": mix["path"],
        "fmt": mix["fmt"],
        "duration_s": mix["duration_s"],
        "lufs": mix["lufs"],
        "true_peak_dbtp": mix["true_peak_dbtp"],
        "lra": mix["lra"],
        "clipped": mix["clipped"],
        "balance": mix["balance"],
        "reverb": mix["reverb"],
        "fonts": list(fonts.values()),
        "missing_presets": missing,
        "fallbacks": fallbacks,
        "muse": {"version": muse_version, "dry_seconds": dry_seconds, "tracks": muse_tracks}
        if has_muse
        else None,
        "parts": parts,
        "wall_s": round(wall_s, 3),
    }
