"""SoundfontStore 的离线测试；HTTP 全部由 MockTransport 提供。"""

from __future__ import annotations

import asyncio
import hashlib
import re
import struct
from collections.abc import Callable, Sequence
from pathlib import Path

import httpx
import pytest

from cgbot.music.errors import MusicError
from cgbot.music.soundfont import (
    DEFAULT_CATALOG,
    MAX_SOUNDFONT_BYTES,
    MIN_SOUNDFONT_BYTES,
    CatalogEntry,
    PresetHeader,
    SoundfontStore,
    _validate_soundfont,
    list_presets,
)

SF3_SIZE = 1024 * 1024


def _sf3(fill: bytes = b"\0") -> bytes:
    """构造满足 RIFF/SF3 结构且达到最小尺寸校验的测试文件。"""
    payload = b"OggS\0\x01vorbis" + fill * 64
    sample_chunk = b"smpl" + struct.pack("<I", len(payload)) + payload
    list_body = b"sdta" + sample_chunk
    list_chunk = b"LIST" + struct.pack("<I", len(list_body)) + list_body
    riff_body = bytearray(b"sfbk" + list_chunk)
    riff_body.extend(fill * (SF3_SIZE - 8 - len(riff_body)))
    return b"RIFF" + struct.pack("<I", len(riff_body)) + bytes(riff_body)


def _entry(
    *,
    data: bytes | None = None,
    urls: Sequence[str] = ("https://one.example/font.sf3",),
    sha256: str | None = None,
) -> CatalogEntry:
    body = data if data is not None else _sf3()
    return CatalogEntry(
        id="test",
        name="Test font",
        license="MIT",
        description="offline test",
        urls=tuple(urls),
        size_bytes=len(body),
        sha256=sha256,
        filename="test.sf3",
        tags=("general",),
        recommended=False,
    )


def _store(
    tmp_path: Path,
    handler: Callable[[httpx.Request], httpx.Response],
    entry: CatalogEntry,
) -> SoundfontStore:
    transport = httpx.MockTransport(handler)
    return SoundfontStore(
        tmp_path / "soundfonts",
        client=httpx.AsyncClient(transport=transport),
        catalog=(entry,),
    )


@pytest.fixture(autouse=True)
def no_retry_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cgbot.music.soundfont._RETRY_BASE_SECONDS", 0.0)


async def test_resume_appends_only_remaining_bytes(tmp_path: Path) -> None:
    body = _sf3()
    split = 64 * 1024
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers["range"] == f"bytes={split}-"
        return httpx.Response(
            206,
            headers={"Content-Range": f"bytes {split}-{len(body) - 1}/{len(body)}"},
            content=body[split:],
        )

    entry = _entry(data=body)
    store = _store(tmp_path, handler, entry)
    part = store.directory / "test.sf3.part"
    part.write_bytes(body[:split])
    progress: list[tuple[str, int, int | None]] = []

    result = await store.ensure("test", progress=lambda *args: progress.append(args))

    assert result.read_bytes() == body
    assert not part.exists()
    assert len(seen) == 1
    assert progress[-1] == ("test", len(body), len(body))


async def test_mirror_fallback_after_retries(tmp_path: Path) -> None:
    body = _sf3()
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.host == "one.example":
            return httpx.Response(503, text="unavailable")
        return httpx.Response(200, content=body)

    entry = _entry(
        data=body,
        urls=("https://one.example/font.sf3", "https://two.example/font.sf3"),
    )
    result = await _store(tmp_path, handler, entry).ensure("test")

    assert result.read_bytes() == body
    assert [httpx.URL(url).host for url in seen] == [
        "one.example",
        "one.example",
        "one.example",
        "two.example",
    ]


async def test_bad_content_is_rejected_and_partial_removed(tmp_path: Path) -> None:
    body = b"<html>captcha</html>" * (SF3_SIZE // 20)
    entry = _entry(data=body)
    store = _store(tmp_path, lambda _request: httpx.Response(200, content=body), entry)

    with pytest.raises(MusicError) as caught:
        await store.ensure("test")

    assert caught.value.code == "bad_soundfont"
    assert not (store.directory / "test.sf3").exists()
    assert not (store.directory / "test.sf3.part").exists()


async def test_checksum_mismatch_is_rejected(tmp_path: Path) -> None:
    body = _sf3()
    entry = _entry(data=body, sha256="00" * 32)
    store = _store(tmp_path, lambda _request: httpx.Response(200, content=body), entry)

    with pytest.raises(MusicError) as caught:
        await store.ensure("test")

    assert caught.value.code == "bad_soundfont"
    assert not (store.directory / "test.sf3.part").exists()


async def test_matching_checksum_is_accepted(tmp_path: Path) -> None:
    body = _sf3()
    entry = _entry(data=body, sha256=hashlib.sha256(body).hexdigest())
    result = await _store(
        tmp_path, lambda _request: httpx.Response(200, content=body), entry
    ).ensure("test")
    assert result.read_bytes() == body


async def test_concurrent_ensure_shares_one_download(tmp_path: Path) -> None:
    body = _sf3()
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=body)

    store = _store(tmp_path, handler, _entry(data=body))
    results = await asyncio.gather(*(store.ensure("test") for _ in range(8)))

    assert calls == 1
    assert results == [store.directory / "test.sf3"] * 8


async def test_short_response_is_resumed_on_retry(tmp_path: Path) -> None:
    body = _sf3()
    split = 128 * 1024
    requests: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        byte_range = request.headers.get("range")
        requests.append(byte_range)
        if byte_range is None:
            return httpx.Response(200, content=body[:split])
        return httpx.Response(
            206,
            headers={"Content-Range": f"bytes {split}-{len(body) - 1}/{len(body)}"},
            content=body[split:],
        )

    result = await _store(tmp_path, handler, _entry(data=body)).ensure("test")

    assert result.read_bytes() == body
    assert requests == [None, f"bytes={split}-"]


async def test_oversized_response_is_rejected_before_writing(tmp_path: Path) -> None:
    entry = _entry()
    store = _store(
        tmp_path,
        lambda _request: httpx.Response(
            200, headers={"Content-Length": str(MAX_SOUNDFONT_BYTES + 1)}
        ),
        entry,
    )
    with pytest.raises(MusicError) as caught:
        await store.ensure("test")
    assert caught.value.code == "download_failed"
    assert not (store.directory / "test.sf3.part").exists()


async def test_unknown_id_does_not_escape_directory(tmp_path: Path) -> None:
    store = SoundfontStore(tmp_path, catalog=())
    with pytest.raises(MusicError) as caught:
        await store.ensure("../secret")
    assert caught.value.code == "unknown_soundfont"


def test_list_and_find_installed_prefer_default_and_include_user_files(
    tmp_path: Path,
) -> None:
    store = SoundfontStore(tmp_path)
    user = tmp_path / "My Piano.sf3"
    user.write_bytes(b"user-owned; list does not read or validate it")
    default = tmp_path / "MuseScore_General.sf3"
    default.write_bytes(b"user-supplied default")

    items = store.list()
    assert any(item.id == "My Piano" and item.installed for item in items)
    assert store.find_installed() == default
    default.unlink()
    assert store.find_installed() == user


def _chunk(chunk_id: bytes, body: bytes, *, pad: bool = True) -> bytes:
    chunk = chunk_id + struct.pack("<I", len(body)) + body
    return chunk + b"\0" if pad and len(body) % 2 else chunk


def _phdr(presets: Sequence[tuple[str, int, int]]) -> bytes:
    records = b"".join(
        struct.pack("<20sHHHIII", name.encode("latin-1"), program, bank, 0, 0, 0, 0)
        for name, bank, program in presets
    )
    # 终止记录 EOP。
    return records + struct.pack("<20sHHHIII", b"EOP", 0, 0, 0, 0, 0, 0)


def _sf2(
    presets: Sequence[tuple[str, int, int]] = (("Grand Piano", 0, 0), ("Drum Kit", 128, 0)),
    *,
    pad_odd: bool = True,
) -> bytes:
    """构造 SF2：16 位 PCM 的 smpl 与 pdta/phdr，填充到最小尺寸。"""
    pcm = b"\x01\x02" * 64 + b"\x03"  # 奇数长度，用于覆盖缺失补位。
    sdta = _chunk(b"LIST", b"sdta" + _chunk(b"smpl", pcm, pad=pad_odd), pad=pad_odd)
    pdta = _chunk(b"LIST", b"pdta" + _chunk(b"phdr", _phdr(presets)))
    info = _chunk(b"LIST", b"INFO" + _chunk(b"ifil", b"\x02\x00\x01\x00"))
    riff_body = bytearray(b"sfbk" + info + sdta + pdta)
    riff_body.extend(_chunk(b"junk", b"\0" * (SF3_SIZE - 8 - len(riff_body) - 8)))
    return b"RIFF" + struct.pack("<I", len(riff_body)) + bytes(riff_body)


def test_validate_accepts_sf2_pcm_and_rejects_non_riff(tmp_path: Path) -> None:
    good = tmp_path / "good.sf2"
    good.write_bytes(_sf2())
    _validate_soundfont(good)

    bad = tmp_path / "bad.sf2"
    bad.write_bytes(b"<html>" + b"x" * SF3_SIZE)
    with pytest.raises(MusicError) as caught:
        _validate_soundfont(bad)
    assert caught.value.code == "bad_soundfont"


def test_validate_rejects_broken_ogg_sample_data(tmp_path: Path) -> None:
    # smpl 以 OggS 开头却不是 Vorbis：既不是合法 SF3，也不能当 SF2 PCM 放行。
    payload = b"OggS\0\x01opus" + b"\0" * 64
    sdta = _chunk(b"LIST", b"sdta" + _chunk(b"smpl", payload))
    riff_body = bytearray(b"sfbk" + sdta)
    riff_body.extend(_chunk(b"junk", b"\0" * (SF3_SIZE - 8 - len(riff_body) - 8)))
    path = tmp_path / "ogg.sf3"
    path.write_bytes(b"RIFF" + struct.pack("<I", len(riff_body)) + bytes(riff_body))
    with pytest.raises(MusicError):
        _validate_soundfont(path)


@pytest.mark.parametrize("pad_odd", [True, False])
def test_list_presets_reads_names_banks_and_programs(tmp_path: Path, pad_odd: bool) -> None:
    path = tmp_path / "font.sf2"
    path.write_bytes(_sf2(pad_odd=pad_odd))

    assert list_presets(path) == [
        PresetHeader(name="Grand Piano", bank=0, program=0),
        PresetHeader(name="Drum Kit", bank=128, program=0),
    ]


def test_list_presets_rejects_non_soundfont(tmp_path: Path) -> None:
    path = tmp_path / "bad.sf2"
    path.write_bytes(b"not a soundfont at all")
    with pytest.raises(MusicError) as caught:
        list_presets(path)
    assert caught.value.code == "bad_soundfont"


async def test_sf2_entry_downloads_and_user_sf2_is_listed(tmp_path: Path) -> None:
    body = _sf2()
    entry = CatalogEntry(
        id="piano",
        name="Piano",
        license="CC0",
        description="offline sf2",
        urls=("https://one.example/piano.sf2",),
        size_bytes=len(body),
        sha256=hashlib.sha256(body).hexdigest(),
        filename="piano.sf2",
        tags=("keys",),
        recommended=True,
    )
    store = _store(tmp_path, lambda _request: httpx.Response(200, content=body), entry)

    assert entry.format == "sf2"
    result = await store.ensure("piano")
    assert result.name == "piano.sf2"
    (store.directory / "Mine.SF2").write_bytes(body)
    ids = {item.id for item in store.list()}
    assert ids == {"piano", "Mine"}
    assert await store.ensure("Mine") == store.directory / "Mine.SF2"


_ALLOWED_TAGS = {"keys", "piano", "epiano", "fm", "orchestra", "chamber", "general", "lite"}


def test_default_catalog_is_well_formed() -> None:
    ids = [entry.id for entry in DEFAULT_CATALOG]
    filenames = [entry.filename for entry in DEFAULT_CATALOG]
    assert len(set(ids)) == len(ids)
    assert len(set(filenames)) == len(filenames)
    assert sum(entry.recommended for entry in DEFAULT_CATALOG) == 1
    for entry in DEFAULT_CATALOG:
        assert entry.format in {"sf2", "sf3"}
        assert entry.urls and all(url.startswith("https://") for url in entry.urls)
        assert entry.size_bytes is not None
        assert MIN_SOUNDFONT_BYTES <= entry.size_bytes <= MAX_SOUNDFONT_BYTES
        assert entry.sha256 is not None and re.fullmatch(r"[0-9a-f]{64}", entry.sha256)
        assert entry.tags and set(entry.tags) <= _ALLOWED_TAGS
        assert entry.license
    # 用户关心的三类音色都至少有一个条目。
    for tag in ("keys", "fm", "chamber"):
        assert any(tag in entry.tags for entry in DEFAULT_CATALOG)
