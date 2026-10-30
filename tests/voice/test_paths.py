from __future__ import annotations

import os
from pathlib import Path

import pytest
from voice_fakes import make_wav

from cgbot.voice.errors import VoicePathError
from cgbot.voice.paths import MAX_WAV_BYTES, PathMapper, PathRule


def _mapper(local: Path) -> PathMapper:
    return PathMapper([PathRule(remote_prefix="/AstrBot/data/temp", local_prefix=str(local))])


def test_rule_requires_absolute_normalised_prefixes() -> None:
    with pytest.raises(ValueError):
        PathRule(remote_prefix="relative", local_prefix="/tmp/x")
    with pytest.raises(ValueError):
        PathRule(remote_prefix="/a/../b", local_prefix="/tmp/x")
    with pytest.raises(ValueError):
        PathRule(remote_prefix="/", local_prefix="/tmp/x")


def test_resolves_wav_under_mapped_prefix(tmp_path: Path) -> None:
    wav = tmp_path / "out.wav"
    make_wav(wav)
    resolved = _mapper(tmp_path).resolve_wav("/AstrBot/data/temp/out.wav")
    assert resolved == wav.resolve()


def test_first_candidate_that_resolves_wins(tmp_path: Path) -> None:
    host_dir = tmp_path / "host"
    host_dir.mkdir()
    make_wav(host_dir / "a.wav")
    mapper = PathMapper(
        [
            PathRule(remote_prefix="/AstrBot/data/temp", local_prefix=str(tmp_path / "missing")),
            PathRule(remote_prefix="/home/klp/qqbot/data/temp", local_prefix=str(host_dir)),
        ]
    )
    resolved = mapper.resolve_wav("/home/klp/qqbot/data/temp/a.wav", "/AstrBot/data/temp/a.wav")
    assert resolved == (host_dir / "a.wav").resolve()


def test_longest_prefix_rule_wins(tmp_path: Path) -> None:
    short_dir = tmp_path / "short"
    long_dir = tmp_path / "long"
    long_dir.mkdir()
    short_dir.mkdir()
    make_wav(long_dir / "x.wav")
    mapper = PathMapper(
        [
            PathRule(remote_prefix="/srv", local_prefix=str(short_dir)),
            PathRule(remote_prefix="/srv/data", local_prefix=str(long_dir)),
        ]
    )
    assert mapper.resolve_wav("/srv/data/x.wav") == (long_dir / "x.wav").resolve()


def test_prefix_must_match_whole_path_component(tmp_path: Path) -> None:
    make_wav(tmp_path / "x.wav")
    mapper = PathMapper([PathRule(remote_prefix="/srv/data", local_prefix=str(tmp_path))])
    with pytest.raises(VoicePathError):
        mapper.resolve_wav("/srv/database/x.wav")


def test_traversal_outside_mapped_root_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    make_wav(tmp_path / "secret.wav")
    mapper = PathMapper([PathRule(remote_prefix="/srv", local_prefix=str(root))])
    with pytest.raises(VoicePathError):
        mapper.resolve_wav("/srv/../../secret.wav")


def test_symlink_escaping_root_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.wav"
    make_wav(outside)
    os.symlink(outside, root / "link.wav")
    mapper = PathMapper([PathRule(remote_prefix="/srv", local_prefix=str(root))])
    with pytest.raises(VoicePathError):
        mapper.resolve_wav("/srv/link.wav")


def test_non_wav_content_is_refused(tmp_path: Path) -> None:
    (tmp_path / "x.wav").write_bytes(b"#!/bin/sh\necho hi\n")
    with pytest.raises(VoicePathError):
        _mapper(tmp_path).resolve_wav("/AstrBot/data/temp/x.wav")


def test_oversized_file_is_refused(tmp_path: Path) -> None:
    big = tmp_path / "big.wav"
    with big.open("wb") as handle:
        handle.write(b"RIFF" + b"\x00" * 4 + b"WAVE")
        handle.truncate(MAX_WAV_BYTES + 1)
    with pytest.raises(VoicePathError):
        _mapper(tmp_path).resolve_wav("/AstrBot/data/temp/big.wav")


def test_no_candidate_resolves_raises(tmp_path: Path) -> None:
    with pytest.raises(VoicePathError):
        _mapper(tmp_path).resolve_wav("/AstrBot/data/temp/absent.wav")


def test_unmapped_remote_path_raises(tmp_path: Path) -> None:
    with pytest.raises(VoicePathError):
        _mapper(tmp_path).resolve_wav("/elsewhere/x.wav")
