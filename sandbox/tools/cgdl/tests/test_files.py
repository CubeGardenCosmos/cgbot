"""文件名、输出目录与原子写入的离线测试。"""

from pathlib import Path

import pytest

from cgdl import files
from cgdl.errors import BAD_INPUT, CgdlError
from cgdl.files import atomic_write, ensure_output_dir, sanitize_stem


def test_sanitize_title_words() -> None:
    assert sanitize_stem("Gymnopédie: No/1 *", 5) == "Gymnopédie_No_1-5"


def test_sanitize_blank_title_falls_back() -> None:
    assert sanitize_stem("   ", 7) == "score-7"


def test_sanitize_no_path_components() -> None:
    stem = sanitize_stem("../../etc", 3)
    assert "/" not in stem
    assert not stem.startswith(".")
    assert stem == "etc-3"


def test_sanitize_truncates_long_titles() -> None:
    assert sanitize_stem("a" * 100, 5) == "a" * 64 + "-5"


def test_ensure_output_dir_requires_absolute(tmp_path: Path) -> None:
    with pytest.raises(CgdlError) as info:
        ensure_output_dir("relative/dir")
    assert info.value.code == BAD_INPUT


def test_ensure_output_dir_creates(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b"
    assert ensure_output_dir(str(target)) == target
    assert target.is_dir()


def test_atomic_write_creates_readable_file(tmp_path: Path) -> None:
    dest = tmp_path / "x.mid"
    atomic_write(dest, b"MThd")
    assert dest.read_bytes() == b"MThd"
    assert dest.stat().st_mode & 0o777 == 0o644
    assert sorted(p.name for p in tmp_path.iterdir()) == ["x.mid"]


def test_atomic_write_overwrites_existing(tmp_path: Path) -> None:
    dest = tmp_path / "x.mid"
    dest.write_bytes(b"old")
    atomic_write(dest, b"new")
    assert dest.read_bytes() == b"new"


def test_atomic_write_cleans_up_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(src: str, dst: str) -> None:
        raise OSError("disk gone")

    monkeypatch.setattr(files.os, "replace", boom)
    with pytest.raises(OSError):
        atomic_write(tmp_path / "x.mid", b"MThd")
    assert list(tmp_path.iterdir()) == []


def test_atomic_write_rejects_bad_name(tmp_path: Path) -> None:
    with pytest.raises(CgdlError) as info:
        atomic_write(tmp_path / "..", b"x")
    assert info.value.code == BAD_INPUT
