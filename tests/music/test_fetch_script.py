"""scripts/fetch_soundfonts.py 的离线测试；HTTP 全部由 MockTransport 提供，不触网。"""

from __future__ import annotations

import importlib.util
import stat
import struct
from pathlib import Path
from types import ModuleType

import httpx
import pytest

from cgbot.music.soundfont import CatalogEntry

SF3_SIZE = 1024 * 1024
_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "fetch_soundfonts.py"


def _sf3(fill: bytes = b"\0") -> bytes:
    """构造满足 RIFF/SF3 结构且达到最小尺寸校验的测试文件。"""
    payload = b"OggS\0\x01vorbis" + fill * 64
    sample_chunk = b"smpl" + struct.pack("<I", len(payload)) + payload
    list_body = b"sdta" + sample_chunk
    list_chunk = b"LIST" + struct.pack("<I", len(list_body)) + list_body
    riff_body = bytearray(b"sfbk" + list_chunk)
    riff_body.extend(fill * (SF3_SIZE - 8 - len(riff_body)))
    return b"RIFF" + struct.pack("<I", len(riff_body)) + bytes(riff_body)


ALPHA_BODY = _sf3(b"a")
BETA_BODY = _sf3(b"b")


def _catalog() -> tuple[CatalogEntry, ...]:
    return (
        CatalogEntry(
            id="alpha",
            name="Alpha",
            license="MIT",
            description="recommended test font",
            urls=("https://mirror.example/alpha.sf3",),
            size_bytes=len(ALPHA_BODY),
            sha256=None,
            filename="alpha.sf3",
            tags=("general",),
            recommended=True,
        ),
        CatalogEntry(
            id="beta",
            name="Beta",
            license="CC-BY-4.0",
            description="lite test font",
            urls=("https://mirror.example/beta.sf3",),
            size_bytes=len(BETA_BODY),
            sha256=None,
            filename="beta.sf3",
            tags=("lite", "general"),
            recommended=False,
        ),
    )


def _transport(
    bodies: dict[str, bytes], requests: list[httpx.Request] | None = None
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        body = bodies.get(request.url.path)
        if body is None:
            return httpx.Response(404)
        range_header = request.headers.get("Range")
        if range_header:
            start = int(range_header.removeprefix("bytes=").removesuffix("-"))
            return httpx.Response(
                206,
                content=body[start:],
                headers={"Content-Range": f"bytes {start}-{len(body) - 1}/{len(body)}"},
            )
        return httpx.Response(200, content=body)

    return httpx.MockTransport(handler)


BODIES = {"/alpha.sf3": ALPHA_BODY, "/beta.sf3": BETA_BODY}


@pytest.fixture(autouse=True)
def offline_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cgbot.music.soundfont.DEFAULT_CATALOG", _catalog())
    monkeypatch.setattr("cgbot.music.soundfont._RETRY_BASE_SECONDS", 0.0)


@pytest.fixture
def script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("fetch_soundfonts", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_default_downloads_only_recommended_with_0644_and_progress(
    script: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    requests: list[httpx.Request] = []
    code = script.main(["--dir", str(tmp_path)], transport=_transport(BODIES, requests))

    assert code == 0
    assert [request.url.path for request in requests] == ["/alpha.sf3"]
    installed = tmp_path / "alpha.sf3"
    assert installed.read_bytes() == ALPHA_BODY
    assert stat.S_IMODE(installed.stat().st_mode) == 0o644
    captured = capsys.readouterr()
    assert "alpha" in captured.err and "MiB" in captured.err
    assert f"OK   alpha  {installed}" in captured.out
    assert not (tmp_path / "beta.sf3").exists()


def test_preset_all_downloads_every_catalog_entry(script: ModuleType, tmp_path: Path) -> None:
    code = script.main(["--dir", str(tmp_path), "--preset", "all"], transport=_transport(BODIES))

    assert code == 0
    assert (tmp_path / "alpha.sf3").read_bytes() == ALPHA_BODY
    assert (tmp_path / "beta.sf3").read_bytes() == BETA_BODY


def test_preset_id_list_downloads_only_named_entries(script: ModuleType, tmp_path: Path) -> None:
    requests: list[httpx.Request] = []
    code = script.main(
        ["--dir", str(tmp_path), "--preset", "beta,beta"],
        transport=_transport(BODIES, requests),
    )

    assert code == 0
    assert [request.url.path for request in requests] == ["/beta.sf3"]
    assert not (tmp_path / "alpha.sf3").exists()


def test_unknown_preset_id_is_a_usage_error(
    script: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as caught:
        script.main(["--dir", str(tmp_path), "--preset", "nope"], transport=_transport(BODIES))

    assert caught.value.code == 2
    assert "unknown SoundFont id: nope" in capsys.readouterr().err


def test_resume_continues_from_existing_part_file(script: ModuleType, tmp_path: Path) -> None:
    split = 300_000
    (tmp_path / "alpha.sf3.part").write_bytes(ALPHA_BODY[:split])
    requests: list[httpx.Request] = []

    code = script.main(["--dir", str(tmp_path)], transport=_transport(BODIES, requests))

    assert code == 0
    assert [request.headers.get("Range") for request in requests] == [f"bytes={split}-"]
    assert (tmp_path / "alpha.sf3").read_bytes() == ALPHA_BODY
    assert not (tmp_path / "alpha.sf3.part").exists()


def test_already_installed_valid_file_is_not_downloaded_again(
    script: ModuleType, tmp_path: Path
) -> None:
    (tmp_path / "alpha.sf3").write_bytes(ALPHA_BODY)
    requests: list[httpx.Request] = []

    code = script.main(["--dir", str(tmp_path)], transport=_transport(BODIES, requests))

    assert code == 0
    assert requests == []


def test_download_failure_exits_non_zero(
    script: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = script.main(["--dir", str(tmp_path)], transport=_transport({}))

    assert code == 1
    assert "FAIL alpha" in capsys.readouterr().err
    assert not (tmp_path / "alpha.sf3").exists()


def test_list_shows_id_size_licence_tags_and_installed_state(
    script: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "beta.sf3").write_bytes(BETA_BODY)

    code = script.main(["--dir", str(tmp_path), "--list"], transport=_transport({}))

    assert code == 0
    lines = capsys.readouterr().out.splitlines()
    alpha_line = next(line for line in lines if line.startswith("alpha*"))
    beta_line = next(line for line in lines if line.startswith("beta "))
    assert "MIT" in alpha_line and "general" in alpha_line and alpha_line.endswith("no")
    assert "CC-BY-4.0" in beta_line and "lite,general" in beta_line and beta_line.endswith("yes")
    assert "recommended default" in lines[-1]


def test_verify_accepts_valid_installed_files_without_network(
    script: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "alpha.sf3").write_bytes(ALPHA_BODY)
    (tmp_path / "beta.sf3").write_bytes(BETA_BODY)
    requests: list[httpx.Request] = []

    code = script.main(["--dir", str(tmp_path), "--verify"], transport=_transport(BODIES, requests))

    assert code == 0
    assert requests == []
    out = capsys.readouterr().out
    assert "OK   alpha" in out and "OK   beta" in out


def test_verify_fails_on_corrupt_file(
    script: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "alpha.sf3").write_bytes(b"not a soundfont")

    code = script.main(["--dir", str(tmp_path), "--verify", "--preset", "alpha"])

    assert code == 1
    assert "FAIL alpha" in capsys.readouterr().err
    assert (tmp_path / "alpha.sf3").read_bytes() == b"not a soundfont"


def test_verify_reports_missing_preset_as_failure(
    script: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = script.main(["--dir", str(tmp_path), "--verify", "--preset", "beta"])

    assert code == 1
    assert "not_installed" in capsys.readouterr().err


def test_verify_without_any_installed_file_exits_non_zero(
    script: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = script.main(["--dir", str(tmp_path), "--verify"])

    assert code == 1
    assert "no installed catalog SoundFonts" in capsys.readouterr().err


def test_print_mount_emits_read_only_bind_line(
    script: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = script.main(["--dir", str(tmp_path), "--print-mount"])

    assert code == 0
    assert capsys.readouterr().out.strip() == f"-v {tmp_path.resolve()}:/opt/soundfonts:ro"


def test_preset_tag_downloads_only_entries_with_that_tag(
    script: ModuleType, tmp_path: Path
) -> None:
    requests: list[httpx.Request] = []
    code = script.main(
        ["--dir", str(tmp_path), "--preset", "lite"], transport=_transport(BODIES, requests)
    )

    assert code == 0
    assert [request.url.path for request in requests] == ["/beta.sf3"]


def test_preset_tokens_mix_tags_ids_and_recommended_without_duplicates(
    script: ModuleType, tmp_path: Path
) -> None:
    requests: list[httpx.Request] = []
    code = script.main(
        ["--dir", str(tmp_path), "--preset", "recommended,general,beta"],
        transport=_transport(BODIES, requests),
    )

    assert code == 0
    assert sorted(request.url.path for request in requests) == ["/alpha.sf3", "/beta.sf3"]


def test_unknown_tag_is_a_usage_error(
    script: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as caught:
        script.main(["--dir", str(tmp_path), "--preset", "lite,fm"], transport=_transport(BODIES))

    assert caught.value.code == 2
    assert "unknown SoundFont id: fm" in capsys.readouterr().err
