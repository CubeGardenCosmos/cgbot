"""乐谱收件箱暂存(ScoreStager)与 NapCat 文件取回(fetch_score)。离线。"""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any

import pytest

from cgbot.coding.contracts import workspace_dir
from cgbot.coding.staging import MAX_STAGED_SCORES, ImageStager, ScoreStager, StageError
from cgbot.music.files import (
    ScoreFetchError,
    declared_size,
    fetch_score,
    score_extension,
)

NOW = 1793318400.0  # 2026-10-30 00:00:00 UTC
MIDI = b"MThd\x00\x00\x00\x06\x00\x00\x00\x01\x00\x60"
XML = b'<?xml version="1.0"?><score-partwise/>'
MXL = b"PK\x03\x04rest"
PNG = b"\x89PNG\r\n\x1a\nimage"


def _stager(root: Path, **kwargs: Any) -> ScoreStager:
    return ScoreStager(root, max_bytes=kwargs.pop("max_bytes", 1024), clock=lambda: NOW, **kwargs)


def _host(root: Path, sandbox_path: str, user: str = "123") -> Path:
    return workspace_dir(root, user) / sandbox_path.removeprefix("/workspace/")


@pytest.mark.parametrize(
    ("data", "extension", "name"),
    [
        (MIDI, "mid", "score-20261030-000000-1.mid"),
        (MIDI, "midi", "score-20261030-000000-1.midi"),
        (MIDI, "MID", "score-20261030-000000-1.mid"),
        (XML, "musicxml", "score-20261030-000000-1.musicxml"),
        (b"\xef\xbb\xbf\n" + XML, "xml", "score-20261030-000000-1.xml"),
        (MXL, "mxl", "score-20261030-000000-1.mxl"),
    ],
)
async def test_stage_generates_name_and_writes_bytes(
    tmp_path: Path, data: bytes, extension: str, name: str
) -> None:
    path = await _stager(tmp_path).stage("123", data, extension)
    assert path == f"/workspace/inbox/{name}"
    target = _host(tmp_path, path)
    assert target.read_bytes() == data
    assert target.stat().st_mode & 0o777 == 0o644
    assert target.parent.stat().st_mode & 0o777 == 0o755


async def test_names_do_not_collide_and_are_per_user(tmp_path: Path) -> None:
    stager = _stager(tmp_path)
    first = await stager.stage("123", MIDI, "mid")
    second = await stager.stage("123", MIDI, "mid")
    other = await stager.stage("456", MIDI, "mid")
    assert first.endswith("-1.mid")
    assert second.endswith("-2.mid")
    assert other.endswith("-1.mid")
    assert _host(tmp_path, other, "456").exists()


@pytest.mark.parametrize(
    ("data", "extension", "code"),
    [
        (b"", "mid", "empty_file"),
        (b"not midi", "mid", "unsupported_score"),
        (b"MThd", "xml", "unsupported_score"),
        (b"plain text", "musicxml", "unsupported_score"),
        (b"MThd" + b"x" * 2000, "mid", "too_large"),
        (MIDI, "exe", "unsupported_score"),
        (MIDI, "../../x", "unsupported_score"),
        (b"MThd", "mxl", "unsupported_score"),
    ],
)
async def test_stage_rejects_bad_input(
    tmp_path: Path, data: bytes, extension: str, code: str
) -> None:
    with pytest.raises(StageError) as caught:
        await _stager(tmp_path).stage("123", data, extension)
    assert caught.value.code == code
    assert not workspace_dir(tmp_path, "123").exists()


@pytest.mark.parametrize("user_id", ["", ".", "..", "../x", "a/b", "a" * 65])
async def test_stage_rejects_unsafe_user_ids(tmp_path: Path, user_id: str) -> None:
    with pytest.raises(StageError) as caught:
        await _stager(tmp_path).stage(user_id, MIDI, "mid")
    assert caught.value.code == "invalid_user_id"


async def test_prune_keeps_newest_scores_and_never_touches_images_or_other_files(
    tmp_path: Path,
) -> None:
    inbox = workspace_dir(tmp_path, "123") / "inbox"
    inbox.mkdir(parents=True)
    old_scores = []
    for index in range(MAX_STAGED_SCORES):
        path = inbox / f"score-20200101-000000-{index + 1}.mid"
        path.write_bytes(MIDI)
        os.utime(path, (index + 1, index + 1))
        old_scores.append(path)
    keep = inbox / "notes.mid"  # 不匹配生成名:不参与清理
    keep.write_bytes(b"mine")
    image = inbox / "20200101-000000-1.png"
    image.write_bytes(PNG)
    os.utime(image, (0, 0))
    await _stager(tmp_path).stage("123", MIDI, "mid")
    assert not old_scores[0].exists()  # 最旧的一份被清理
    assert old_scores[1].exists()
    assert keep.read_bytes() == b"mine"
    assert image.exists()


async def test_image_pruning_ignores_scores(tmp_path: Path) -> None:
    stager = ScoreStager(tmp_path, max_bytes=1024, clock=lambda: NOW)
    score = await stager.stage("123", MIDI, "mid")

    async def fetch(url: str) -> bytes:
        return PNG

    images = ImageStager(tmp_path, fetch=fetch, clock=lambda: NOW)
    await images.stage("123", "https://img.example/a.png")
    assert _host(tmp_path, score).exists()


@pytest.mark.parametrize("component", ["users", "123", "inbox"])
async def test_rejects_directory_symlinks(tmp_path: Path, component: str) -> None:
    target = tmp_path / "outside"
    target.mkdir()
    workspace = workspace_dir(tmp_path, "123")
    if component == "users":
        (tmp_path / "users").symlink_to(target, target_is_directory=True)
    elif component == "123":
        (tmp_path / "users").mkdir()
        (tmp_path / "users" / "123").symlink_to(target, target_is_directory=True)
    else:
        workspace.mkdir(parents=True)
        (workspace / "inbox").symlink_to(target, target_is_directory=True)
    with pytest.raises(StageError) as caught:
        await _stager(tmp_path).stage("123", MIDI, "mid")
    assert caught.value.code == "unsafe_path"
    assert list(target.iterdir()) == []


async def test_name_collision_with_symlink_is_not_followed(tmp_path: Path) -> None:
    inbox = workspace_dir(tmp_path, "123") / "inbox"
    inbox.mkdir(parents=True)
    secret = tmp_path / "secret"
    secret.write_bytes(b"private")
    (inbox / "score-20261030-000000-1.mid").symlink_to(secret)
    path = await _stager(tmp_path).stage("123", MIDI, "mid")
    assert path.endswith("-2.mid")
    assert secret.read_bytes() == b"private"


# ---------------------------------------------------------------- 扩展名与大小


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"file": "a.mid"}, "mid"),
        ({"file": "A.MIDI"}, "midi"),
        ({"file": "song.musicxml"}, "musicxml"),
        ({"file": "x.xml"}, "xml"),
        ({"file": "x.mxl"}, "mxl"),
        ({"name": "x.mid", "file": "opaque-id"}, "mid"),
        ({"file_name": "x.mid"}, "mid"),
        ({"file": "x.mp3"}, None),
        ({"file": "midi"}, None),
        ({"file": "x.mid.exe"}, None),
        ({}, None),
        ({"file": 3}, None),
    ],
)
def test_score_extension(data: dict[str, object], expected: str | None) -> None:
    assert score_extension(data) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(10, 10), ("2048", 2048), ("x", None), (True, None), (None, None), (-1, -1)],
)
def test_declared_size(value: object, expected: int | None) -> None:
    assert declared_size({"file_size": value}) == expected


# ---------------------------------------------------------------- fetch_score


class FakeNapCat:
    def __init__(self, response: dict[str, object] | Exception) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def __call__(
        self, action: str, params: dict[str, object] | None = None, timeout_s: float = 10.0
    ) -> dict[str, object]:
        self.calls.append((action, dict(params or {})))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _no_fetch(url: str) -> Any:
    raise AssertionError("must not fetch")


async def test_fetch_score_base64() -> None:
    napcat = FakeNapCat({"data": {"base64": base64.b64encode(MIDI).decode()}})
    data = await fetch_score(napcat, {"file": "a.mid", "file_id": "fid"}, max_bytes=100)
    assert data == MIDI
    assert napcat.calls == [("get_file", {"file_id": "fid"})]


async def test_fetch_score_local_path(tmp_path: Path) -> None:
    local = tmp_path / "cache.mid"
    local.write_bytes(MIDI)
    napcat = FakeNapCat({"data": {"file": str(local)}})
    assert await fetch_score(napcat, {"file": "a.mid", "file_id": "f"}, max_bytes=100) == MIDI
    assert napcat.calls[0][1] == {"file_id": "f"}


async def test_fetch_score_uses_file_param_without_file_id(tmp_path: Path) -> None:
    local = tmp_path / "cache.mid"
    local.write_bytes(MIDI)
    napcat = FakeNapCat({"data": {"file": str(local)}})
    await fetch_score(napcat, {"file": "a.mid"}, max_bytes=100)
    assert napcat.calls[0][1] == {"file": "a.mid"}


async def test_fetch_score_url_from_get_file_goes_through_injected_fetch() -> None:
    urls: list[str] = []

    async def fetch(url: str) -> bytes:
        urls.append(url)
        return MIDI

    napcat = FakeNapCat({"data": {"file": "/not/shared/here", "url": "https://files.example/a"}})
    data = await fetch_score(napcat, {"file": "a.mid", "file_id": "f"}, max_bytes=100, fetch=fetch)
    assert data == MIDI
    assert urls == ["https://files.example/a"]


async def test_fetch_score_falls_back_to_segment_url_when_get_file_fails() -> None:
    urls: list[str] = []

    async def fetch(url: str) -> bytes:
        urls.append(url)
        return MIDI

    napcat = FakeNapCat(TimeoutError())
    data = await fetch_score(
        napcat,
        {"file": "a.mid", "file_id": "f", "url": "https://files.example/seg"},
        max_bytes=100,
        fetch=fetch,
    )
    assert data == MIDI
    assert urls == ["https://files.example/seg"]


async def test_fetch_score_declared_size_checked_before_any_call() -> None:
    napcat = FakeNapCat({"data": {}})
    with pytest.raises(ScoreFetchError) as caught:
        await fetch_score(
            napcat, {"file": "a.mid", "file_id": "f", "file_size": "101"}, max_bytes=100
        )
    assert caught.value.code == "too_large"
    assert napcat.calls == []


async def test_fetch_score_reported_size_and_local_size_enforced(tmp_path: Path) -> None:
    napcat = FakeNapCat({"data": {"file_size": 5000, "base64": "AAAA"}})
    with pytest.raises(ScoreFetchError) as reported:
        await fetch_score(napcat, {"file": "a.mid", "file_id": "f"}, max_bytes=100)
    assert reported.value.code == "too_large"

    local = tmp_path / "big.mid"
    local.write_bytes(MIDI + b"x" * 500)
    napcat = FakeNapCat({"data": {"file": str(local)}})
    with pytest.raises(ScoreFetchError) as on_disk:
        await fetch_score(napcat, {"file": "a.mid", "file_id": "f"}, max_bytes=100)
    assert on_disk.value.code == "too_large"


async def test_fetch_score_oversized_base64_rejected_without_decoding() -> None:
    napcat = FakeNapCat({"data": {"base64": "A" * 10_000}})
    with pytest.raises(ScoreFetchError) as caught:
        await fetch_score(napcat, {"file": "a.mid", "file_id": "f"}, max_bytes=100)
    assert caught.value.code == "too_large"


async def test_fetch_score_oversized_download_rejected() -> None:
    async def fetch(url: str) -> bytes:
        return MIDI + b"x" * 500

    napcat = FakeNapCat({"data": {"url": "https://files.example/a"}})
    with pytest.raises(ScoreFetchError) as caught:
        await fetch_score(napcat, {"file": "a.mid", "file_id": "f"}, max_bytes=100, fetch=fetch)
    assert caught.value.code == "too_large"


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1/a", "http://10.0.0.5/a", "ftp://x/a", "https://u:p@x/a", ""]
)
async def test_fetch_score_blocks_unsafe_urls_before_fetching(url: str) -> None:
    napcat = FakeNapCat({"data": {"url": url}})
    with pytest.raises(ScoreFetchError) as caught:
        await fetch_score(napcat, {"file": "a.mid", "file_id": "f"}, max_bytes=100, fetch=_no_fetch)
    assert caught.value.code == "fetch_failed"


async def test_fetch_score_nothing_available() -> None:
    napcat = FakeNapCat({"data": {}})
    with pytest.raises(ScoreFetchError) as caught:
        await fetch_score(napcat, {"file": "a.mid", "file_id": "f"}, max_bytes=100)
    assert caught.value.code == "fetch_failed"
    with pytest.raises(ScoreFetchError):
        await fetch_score(FakeNapCat({}), {"name": "x.mid"}, max_bytes=100)
