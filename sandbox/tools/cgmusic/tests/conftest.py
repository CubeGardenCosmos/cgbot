"""测试夹具:隔离 TMPDIR,并把 CGSYNTH_BIN 指向假 cgsynth 包装脚本。"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

FAKE_PY = Path(__file__).with_name("fake_cgsynth.py")


@dataclass
class FakeCgsynth:
    binary: Path
    muse_binary: Path
    log: Path

    def events(self) -> list[dict[str, Any]]:
        lines = self.log.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    def calls(self) -> list[dict[str, Any]]:
        return [event for event in self.events() if event["event"] == "start"]

    def ends(self) -> list[dict[str, Any]]:
        return [event for event in self.events() if event["event"] == "end"]

    def intervals(self) -> dict[str, tuple[float, float]]:
        """完成渲染的部件 -> (开始时刻, 结束时刻),用于验证并发/串行。"""
        starts: dict[str, float] = {}
        spans: dict[str, tuple[float, float]] = {}
        for event in self.events():
            if event["event"] == "start":
                starts[event["part"]] = event["t"]
            elif event["event"] == "end":
                spans[event["part"]] = (starts[event["part"]], event["t"])
        return spans


@pytest.fixture
def tmp_base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base = tmp_path / "tmp"
    base.mkdir()
    monkeypatch.setenv("TMPDIR", str(base))
    return base


@pytest.fixture
def fake_cgsynth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeCgsynth:
    for key in list(os.environ):
        if key.startswith("FAKE_CGSYNTH_"):
            monkeypatch.delenv(key, raising=False)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    binary = _fake_binary(bindir / "cgsynth")
    muse_binary = _fake_binary(bindir / "mscore-cgsynth")
    log = tmp_path / "fake-cgsynth.log"
    log.touch()
    monkeypatch.setenv("CGSYNTH_BIN", str(binary))
    monkeypatch.setenv("CGSYNTH_MUSE_BIN", str(muse_binary))
    monkeypatch.delenv("CGMUSIC_MUSE_HOME", raising=False)
    monkeypatch.setenv("FAKE_CGSYNTH_LOG", str(log))
    return FakeCgsynth(binary=binary, muse_binary=muse_binary, log=log)


def _fake_binary(path: Path) -> Path:
    path.write_text(
        f'#!/bin/sh\nexport FAKE_CGSYNTH_BINARY="{path}"\n'
        f'exec "{sys.executable}" "{FAKE_PY}" "$@"\n',
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path
