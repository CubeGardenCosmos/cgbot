"""cgsynth 子进程:定位二进制、同步查询与并发渲染(任一失败即终止其余进程)。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cgmusic.errors import SYNTH_FAILED, CgmusicError, from_cgsynth_stderr

BIN_ENV = "CGSYNTH_BIN"


@dataclass(frozen=True)
class RenderCall:
    key: str
    argv: list[str]


@dataclass(frozen=True)
class RenderResult:
    report: dict[str, Any]
    wall_s: float


class _Cancelled(Exception):
    """任务因首个失败被取消,不作为独立错误上报。"""


def locate() -> str:
    override = os.environ.get(BIN_ENV)
    if override:
        if not (Path(override).is_file() and os.access(override, os.X_OK)):
            raise CgmusicError(SYNTH_FAILED, f"{BIN_ENV} 不是可执行文件: {override}")
        return override
    found = shutil.which("cgsynth")
    if found is None:
        raise CgmusicError(SYNTH_FAILED, f"找不到 cgsynth: 请加入 PATH 或设置 {BIN_ENV}")
    return found


def parse_stdout(text: str) -> dict[str, Any]:
    for line in reversed(text.strip().splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    raise CgmusicError(SYNTH_FAILED, "cgsynth 没有输出 JSON")


def run_json(binary: str, args: list[str]) -> dict[str, Any]:
    try:
        proc = subprocess.run([binary, *args], capture_output=True, encoding="utf-8", check=False)
    except OSError as exc:
        raise CgmusicError(SYNTH_FAILED, f"无法启动 cgsynth: {exc}") from exc
    if proc.returncode != 0:
        raise from_cgsynth_stderr(proc.stderr, proc.returncode)
    return parse_stdout(proc.stdout)


class _Group:
    """一个渲染批次的存活子进程集合;取消时终止所有仍在运行的 cgsynth。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._live: set[subprocess.Popen[str]] = set()
        self._cancelled = False

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            live = list(self._live)
        for proc in live:
            proc.kill()

    def run(self, argv: list[str]) -> RenderResult:
        started = time.monotonic()
        with self._lock:
            if self._cancelled:
                raise _Cancelled
            try:
                proc = subprocess.Popen(
                    argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8"
                )
            except OSError as exc:
                raise CgmusicError(SYNTH_FAILED, f"无法启动 cgsynth: {exc}") from exc
            self._live.add(proc)
        try:
            stdout, stderr = proc.communicate()
        finally:
            with self._lock:
                self._live.discard(proc)
        with self._lock:
            cancelled = self._cancelled
        if cancelled:
            raise _Cancelled
        if proc.returncode != 0:
            raise from_cgsynth_stderr(stderr, proc.returncode)
        return RenderResult(parse_stdout(stdout), round(time.monotonic() - started, 3))


def render_all(calls: list[RenderCall], workers: int) -> dict[str, RenderResult]:
    """并发执行渲染;第一个错误取消其余任务并原样抛出该错误。"""
    group = _Group()
    results: dict[str, RenderResult] = {}
    first: CgmusicError | None = None
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="cgsynth")
    try:
        pending: dict[Future[RenderResult], str] = {
            pool.submit(group.run, call.argv): call.key for call in calls
        }
        for future in as_completed(pending):
            try:
                results[pending[future]] = future.result()
            except _Cancelled:
                continue
            except Exception as exc:
                if first is None:
                    first = exc if isinstance(exc, CgmusicError) else CgmusicError(
                        SYNTH_FAILED, str(exc)
                    )
                    group.cancel()
                    for other in pending:
                        other.cancel()
    except BaseException:
        group.cancel()
        raise
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    if first is not None:
        raise first
    return results
