"""可自动下载、校验和断点续传的 SF2/SF3 SoundFont 仓库。"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import struct
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

import httpx

from cgbot.music.errors import MusicError

MIN_SOUNDFONT_BYTES = 1024 * 1024
MAX_SOUNDFONT_BYTES = 600 * 1024 * 1024
_DOWNLOAD_ATTEMPTS = 3
_RETRY_BASE_SECONDS = 0.25
_HTTP_TIMEOUT_SECONDS = 60.0

ProgressCallback = Callable[[str, int, int | None], None]


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    id: str
    name: str
    license: str
    description: str
    urls: tuple[str, ...]
    size_bytes: int | None
    sha256: str | None
    filename: str
    tags: tuple[str, ...]
    recommended: bool

    @property
    def format(self) -> str:
        """文件格式（由文件名后缀决定）：``sf3`` 或 ``sf2``。"""
        return Path(self.filename).suffix.lstrip(".").lower()


@dataclass(frozen=True, slots=True)
class SoundfontInfo:
    id: str
    name: str
    license: str
    size_bytes: int | None
    installed: bool
    path: Path | None
    description: str
    tags: tuple[str, ...]
    recommended: bool


# 大小与 sha256 均来自 2026-11-12 对真实下载文件的计算（HEAD/Range 已确认镜像可达）。
# GitHub 来源的 URL 固定到提交 SHA；sha256 保证内容不随上游变动。
# 许可证原文与证据见 docs/reference/soundfonts.md；仓库只下载，不分发音色文件。
# tags：keys/piano/epiano 键盘；fm 为 DX7 类 FM；orchestra/chamber 管弦与室内乐；general；lite。
DEFAULT_CATALOG: tuple[CatalogEntry, ...] = (
    CatalogEntry(
        id="musescore-general",
        name="MuseScore General",
        license="MIT (samples include public-domain/CC0 material)",
        description="MuseScore 官方高质量 General MIDI 音色，Ogg Vorbis 压缩。",
        urls=(
            "https://ftp.osuosl.org/pub/musescore/soundfont/MuseScore_General/"
            "MuseScore_General.sf3",
        ),
        size_bytes=39_900_972,
        sha256="5b85b6c2c61d10b2b91cddd41efcce7b25cd31c8271d511c73afafbef20b6fa3",
        filename="MuseScore_General.sf3",
        tags=("general",),
        recommended=True,
    ),
    CatalogEntry(
        id="musescore-general-lite",
        name="MuseScore General Lite",
        license="MIT (samples include public-domain/CC0 material)",
        description="MuseScore General 的轻量版本，保留完整 GM 乐器覆盖。",
        urls=("https://people.debian.org/~tg/sf/MuseScore_General_Lite.sf3",),
        size_bytes=39_978_561,
        sha256="916aaca6b0eb9f9083eb42399614acb2ff4ec72ce28dffff1814592a9696b479",
        filename="MuseScore_General_Lite.sf3",
        tags=("general", "lite"),
        recommended=False,
    ),
    CatalogEntry(
        id="musescore-general-full",
        name="MuseScore General Full",
        license="MIT (samples include public-domain/CC0 material)",
        description="MuseScore General 完整版（HQ 构建），约 82 MiB。",
        urls=("https://people.debian.org/~tg/sf/MuseScore_General_Full.sf3",),
        size_bytes=85_967_114,
        sha256="f9a37b36655f23e167c66ec91bef6097c8e99be29286219a54062919eb41aeae",
        filename="MuseScore_General_Full.sf3",
        tags=("general",),
        recommended=False,
    ),
    CatalogEntry(
        id="fluidr3mono-gm",
        name="FluidR3 Mono GM",
        license="MIT",
        description="经典 FluidR3 的单声道 GM 版本，资源占用较低。",
        urls=("https://people.debian.org/~tg/sf/FluidR3Mono_GM.sf3",),
        size_bytes=23_614_353,
        sha256="cda013d8c370a48ae8dad271e761078d2e77455488dabdedbfbe5fc76a38c682",
        filename="FluidR3Mono_GM.sf3",
        tags=("general", "lite"),
        recommended=False,
    ),
    CatalogEntry(
        id="generaluser-gs",
        name="GeneralUser GS",
        license="GeneralUser GS License v2.0 (free use; sample provenance undocumented)",
        description="S. Christian Collins 的 GM/GS 音色（SF2）：钢琴、电钢、大键琴均衡。",
        urls=(
            "https://raw.githubusercontent.com/mrbumpy409/GeneralUser-GS/"
            "684543d5e5efaef08d02be50dcda8d552478fa60/GeneralUser-GS.sf2",
        ),
        size_bytes=32_319_396,
        sha256="9575028c7a1f589f5770fccc8cff2734566af40cd26ed836944e9a5152688cfe",
        filename="GeneralUser-GS.sf2",
        tags=("general", "keys", "piano", "epiano"),
        recommended=False,
    ),
    CatalogEntry(
        id="masterpiece-gm",
        name="Masterpiece GM",
        license="CC0 1.0 (as declared by rKhive; sample provenance not independently verified)",
        description="rKhive 的 GM 音色（SF2）；含 Yamaha 9ft 三角钢琴与 Unison 木管。",
        urls=(
            "https://raw.githubusercontent.com/bratpeki/soundfonts/"
            "e1dcd9da45cc7454b4e3bfae613f4020d6a0b4f7/SF2/GM/Masterpiece.sf2",
        ),
        size_bytes=29_208_234,
        sha256="52d854c93853ec97380af351595fdcce0571242763b7879fd96caac6f86aaf79",
        filename="Masterpiece.sf2",
        tags=("general", "keys", "piano"),
        recommended=False,
    ),
    CatalogEntry(
        id="dx7-rom1a",
        name="Yamaha DX7 ROM 1A",
        license="The Unlicense (repo declaration; Yamaha factory voices rendered)",
        description="DX7 出厂 ROM1 A 组 32 个 FM 音色（SF2；DX9 为其子集）：E.PIANO、BASS 等。",
        urls=(
            "https://raw.githubusercontent.com/Caskexe/DX/"
            "895a568ac033bbbcfd6347ab5100c0ddfd823bfe/DX7/Factory%20ROM/Yamaha%20DX7%20ROM%201A.sf2",
        ),
        size_bytes=18_219_796,
        sha256="9a725f89866e1e4c8b43542493d5a515adb2f13f69166d258fba3cc122cce708",
        filename="Yamaha_DX7_ROM_1A.sf2",
        tags=("fm", "epiano"),
        recommended=False,
    ),
    CatalogEntry(
        id="neo-orchestra",
        name="Neo Orchestra Realistic",
        license="CC0 1.0 (repo declaration; mixed upstream sources, unverified)",
        description="紧凑室内乐/管弦 GM 音色（SF2，约 13 MiB）：VSCO-2 CE 小提琴、弦乐、木管。",
        urls=(
            "https://raw.githubusercontent.com/NeoSoundFonts/neo-orchestra-hq-realistic-sf2/"
            "7aad23dd932a09ad47db55dfc0030403db68ec4f/Neo%20Orch%20Realistic%20SF2.sf2",
        ),
        size_bytes=13_886_634,
        sha256="4ab9c03ebdbdccde19b72531be27c02cb39df8c05b4bdc132ebaf54d16173586",
        filename="Neo_Orch_Realistic.sf2",
        tags=("orchestra", "chamber", "general"),
        recommended=False,
    ),
    CatalogEntry(
        id="timgm6mb",
        name="TimGM6mb",
        license="GPL-2.0",
        description="MuseScore 1 使用的极小 GM 音色（SF2，约 5.7 MiB），仅作兜底。",
        urls=(
            "https://raw.githubusercontent.com/bratpeki/soundfonts/"
            "e1dcd9da45cc7454b4e3bfae613f4020d6a0b4f7/SF2/GM/TimGM.sf2",
        ),
        size_bytes=5_969_788,
        sha256="c5378b62028c920cb11e4803327983fee2f2cdff5dc89c708e39da417e51c854",
        filename="TimGM6mb.sf2",
        tags=("general", "lite"),
        recommended=False,
    ),
)


_SUFFIXES = (".sf3", ".sf2")
_PHDR_RECORD = 38
_MAX_PDTA_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class PresetHeader:
    """phdr 中的一条预设：名称、MIDI bank 与 program。"""

    name: str
    bank: int
    program: int


def _user_fonts(directory: Path) -> list[Path]:
    """目录内全部 .sf3/.sf2 文件（扩展名大小写不敏感），按文件名排序。"""
    return sorted(
        (
            path
            for path in directory.iterdir()
            if path.suffix.lower() in _SUFFIXES and path.is_file()
        ),
        key=lambda item: item.name.casefold(),
    )


def _riff_chunks(stream: BinaryIO, start: int, end: int) -> Iterator[tuple[bytes, int, int]]:
    """遍历 [start, end) 内的 RIFF 子块，产出 (id, 数据偏移, 数据大小)。"""
    offset = start
    while offset + 8 <= end:
        stream.seek(offset)
        header = stream.read(8)
        if len(header) != 8:
            return
        chunk_size = struct.unpack("<I", header[4:])[0]
        data_offset = offset + 8
        if chunk_size > end - data_offset:
            raise MusicError("bad_soundfont", "SoundFont contains a truncated RIFF chunk")
        yield header[:4], data_offset, chunk_size
        offset = data_offset + chunk_size
        if chunk_size & 1:
            # 规范要求补 1 字节；部分 SF3 写入器省略补位，按下一个块头是否合法判断。
            stream.seek(offset)
            if not re.fullmatch(rb"[A-Za-z0-9 ]{4}", stream.read(4)):
                offset += 1


def _open_riff(stream: BinaryIO, size: int) -> None:
    header = stream.read(12)
    if len(header) != 12 or header[:4] != b"RIFF" or header[8:] != b"sfbk":
        raise MusicError("bad_soundfont", "Downloaded file is not a SoundFont (RIFF sfbk)")
    if struct.unpack("<I", header[4:8])[0] + 8 != size:
        raise MusicError("bad_soundfont", "SoundFont RIFF size does not match the file")


def _validate_soundfont(path: Path) -> None:
    """检查 RIFF sfbk 结构与 ``sdta/smpl``：Ogg Vorbis 为 SF3，否则按 SF2 的 PCM 样本接受。"""
    try:
        size = path.stat().st_size
        if not MIN_SOUNDFONT_BYTES <= size <= MAX_SOUNDFONT_BYTES:
            raise MusicError("bad_soundfont", "SoundFont size is outside the supported range")
        with path.open("rb") as stream:
            _open_riff(stream, size)
            for chunk_id, data_offset, chunk_size in _riff_chunks(stream, 12, size):
                if chunk_id != b"LIST" or chunk_size < 4:
                    continue
                stream.seek(data_offset)
                if stream.read(4) == b"sdta":
                    _validate_sample_data(stream, data_offset + 4, data_offset + chunk_size)
                    return
    except MusicError:
        raise
    except OSError as exc:
        raise MusicError("bad_soundfont", "SoundFont could not be read") from exc
    raise MusicError("bad_soundfont", "SoundFont has no sample data")


def _validate_sample_data(stream: BinaryIO, start: int, end: int) -> None:
    # 仅读取 sdta 开头的少量数据；SF3 的 smpl 以 OggS 开头，SF2 为 16 位 PCM。
    stream.seek(start)
    chunk_header = stream.read(8)
    if len(chunk_header) != 8 or chunk_header[:4] != b"smpl":
        raise MusicError("bad_soundfont", "SoundFont has no sample chunk")
    sample_size = struct.unpack("<I", chunk_header[4:])[0]
    if sample_size < 8 or sample_size > end - start - 8:
        raise MusicError("bad_soundfont", "SoundFont sample chunk is invalid")
    sample_head = stream.read(min(sample_size, 256))
    if sample_head.startswith(b"OggS") and b"\x01vorbis" not in sample_head:
        raise MusicError("bad_soundfont", "SoundFont Ogg stream is not Vorbis encoded")


def list_presets(path: Path) -> list[PresetHeader]:
    """纯 Python 读取 pdta/phdr，返回全部预设（不含终止记录 EOP），供诊断与测试使用。"""
    try:
        size = path.stat().st_size
        with path.open("rb") as stream:
            _open_riff(stream, size)
            for chunk_id, data_offset, chunk_size in _riff_chunks(stream, 12, size):
                if chunk_id != b"LIST" or chunk_size < 4:
                    continue
                stream.seek(data_offset)
                if stream.read(4) != b"pdta":
                    continue
                for sub_id, sub_offset, sub_size in _riff_chunks(
                    stream, data_offset + 4, data_offset + chunk_size
                ):
                    if sub_id == b"phdr":
                        return _parse_phdr(stream, sub_offset, sub_size)
                raise MusicError("bad_soundfont", "SoundFont has no phdr chunk")
    except OSError as exc:
        raise MusicError("bad_soundfont", "SoundFont could not be read") from exc
    raise MusicError("bad_soundfont", "SoundFont has no pdta chunk")


def _parse_phdr(stream: BinaryIO, offset: int, size: int) -> list[PresetHeader]:
    if size % _PHDR_RECORD or size > _MAX_PDTA_BYTES:
        raise MusicError("bad_soundfont", "SoundFont phdr chunk is invalid")
    stream.seek(offset)
    data = stream.read(size)
    if len(data) != size:
        raise MusicError("bad_soundfont", "SoundFont phdr chunk is truncated")
    presets: list[PresetHeader] = []
    for start in range(0, size - _PHDR_RECORD, _PHDR_RECORD):  # 最后一条为 EOP
        record = data[start : start + _PHDR_RECORD]
        name = record[:20].split(b"\0", 1)[0].decode("latin-1").strip()
        program, bank = struct.unpack("<HH", record[20:24])
        presets.append(PresetHeader(name=name, bank=bank, program=program))
    return presets


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _check_catalog_file(path: Path, entry: CatalogEntry) -> None:
    """结构、大小与（若有）sha256 校验；失败抛出 MusicError。"""
    _validate_soundfont(path)
    if entry.size_bytes is not None and path.stat().st_size != entry.size_bytes:
        raise MusicError("bad_soundfont", "SoundFont size does not match the catalog")
    if entry.sha256 is not None and _hash_file(path).lower() != entry.sha256.lower():
        raise MusicError("bad_soundfont", "SoundFont checksum does not match")


class SoundfontStore:
    def __init__(
        self,
        directory: Path,
        *,
        client: httpx.AsyncClient | None = None,
        catalog: Sequence[CatalogEntry] | None = None,
    ) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self._client = client
        self._catalog = tuple(catalog) if catalog is not None else DEFAULT_CATALOG
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def catalog(self) -> tuple[CatalogEntry, ...]:
        return self._catalog

    def list(self) -> list[SoundfontInfo]:
        result: list[SoundfontInfo] = []
        known_paths: set[Path] = set()
        for entry in self._catalog:
            path = self.directory / entry.filename
            known_paths.add(path)
            installed = path.is_file()
            actual_size = path.stat().st_size if installed else entry.size_bytes
            result.append(
                SoundfontInfo(
                    id=entry.id,
                    name=entry.name,
                    license=entry.license,
                    size_bytes=actual_size,
                    installed=installed,
                    path=path if installed else None,
                    description=entry.description,
                    tags=entry.tags,
                    recommended=entry.recommended,
                )
            )
        for path in _user_fonts(self.directory):
            if path in known_paths or not path.is_file():
                continue
            result.append(
                SoundfontInfo(
                    id=path.stem,
                    name=path.stem,
                    license="user-supplied",
                    size_bytes=path.stat().st_size,
                    installed=True,
                    path=path,
                    description="用户自行放入音色目录的 SF2/SF3 文件。",
                    tags=(),
                    recommended=False,
                )
            )
        return result

    def default_id(self) -> str | None:
        """目录中 recommended=True 的条目 id；没有则为 None。"""
        return next((item.id for item in self._catalog if item.recommended), None)

    async def ensure(
        self, sf_id: str | None = None, *, progress: ProgressCallback | None = None
    ) -> Path:
        selected_id = sf_id or self.default_id()
        if selected_id is None:
            raise MusicError("unknown_soundfont", "No recommended SoundFont is configured")
        entry = next((item for item in self._catalog if item.id == selected_id), None)
        if entry is None:
            # 用户自定义音色可以通过其文件名（不含扩展名）直接使用。
            supplied = next(
                (path for path in _user_fonts(self.directory) if path.stem == selected_id),
                None,
            )
            if supplied is not None:
                await asyncio.to_thread(_validate_soundfont, supplied)
                return supplied
            raise MusicError("unknown_soundfont", f"Unknown SoundFont: {selected_id}")

        lock = self._locks.setdefault(selected_id, asyncio.Lock())
        async with lock:
            destination = self.directory / entry.filename
            if destination.is_file():
                try:
                    await asyncio.to_thread(_validate_soundfont, destination)
                    return destination
                except MusicError:
                    # 只重建目录中已损坏的下载目标。
                    await asyncio.to_thread(destination.unlink, missing_ok=True)
            return await self._download(entry, destination, progress)

    def find_installed(self) -> Path | None:
        default = next((item for item in self._catalog if item.id == self.default_id()), None)
        if default is not None:
            path = self.directory / default.filename
            if path.is_file():
                return path
        return next(iter(_user_fonts(self.directory)), None)

    async def verify(self, sf_id: str) -> Path:
        """校验已安装的目录条目；只读，不下载、不修改文件。"""
        entry = next((item for item in self._catalog if item.id == sf_id), None)
        if entry is None:
            raise MusicError("unknown_soundfont", f"Unknown SoundFont: {sf_id}")
        destination = self.directory / entry.filename
        if not destination.is_file():
            raise MusicError("not_installed", f"SoundFont is not installed: {sf_id}")
        await asyncio.to_thread(_check_catalog_file, destination, entry)
        return destination

    @asynccontextmanager
    async def _http_client(self) -> AsyncIterator[httpx.AsyncClient]:
        if self._client is not None:
            yield self._client
            return
        async with httpx.AsyncClient(
            timeout=_HTTP_TIMEOUT_SECONDS, follow_redirects=True
        ) as client:
            yield client

    async def _download(
        self, entry: CatalogEntry, destination: Path, progress: ProgressCallback | None
    ) -> Path:
        last_error: Exception | None = None
        async with self._http_client() as client:
            for url in entry.urls:
                for attempt in range(_DOWNLOAD_ATTEMPTS):
                    try:
                        return await self._download_attempt(
                            client, entry, url, destination, progress
                        )
                    except (httpx.HTTPError, OSError, MusicError) as exc:
                        last_error = exc
                        if attempt + 1 < _DOWNLOAD_ATTEMPTS:
                            await asyncio.sleep(_RETRY_BASE_SECONDS * (2**attempt))
        if isinstance(last_error, MusicError):
            raise last_error
        raise MusicError("download_failed", f"Could not download SoundFont: {entry.id}")

    async def _download_attempt(
        self,
        client: httpx.AsyncClient,
        entry: CatalogEntry,
        url: str,
        destination: Path,
        progress: ProgressCallback | None,
    ) -> Path:
        part = destination.with_name(destination.name + ".part")
        offset = (await asyncio.to_thread(part.stat)).st_size if part.exists() else 0
        if offset > MAX_SOUNDFONT_BYTES:
            await asyncio.to_thread(part.unlink, missing_ok=True)
            offset = 0
        if entry.size_bytes is not None and offset == entry.size_bytes:
            return await self._commit(part, destination, entry)

        headers = {"Range": f"bytes={offset}-"} if offset else {}
        async with client.stream("GET", url, headers=headers) as response:
            if response.status_code == 416:
                # 陈旧或越界的续传文件不能继续追加。
                await asyncio.to_thread(part.unlink, missing_ok=True)
                raise MusicError("download_failed", "SoundFont resume position was rejected")
            if response.status_code not in (200, 206):
                raise MusicError(
                    "download_failed", f"SoundFont mirror returned HTTP {response.status_code}"
                )
            content_length = response.headers.get("Content-Length")
            if content_length is not None and content_length.isdigit():
                remaining = int(content_length)
                if offset + remaining > MAX_SOUNDFONT_BYTES:
                    raise MusicError("download_failed", "SoundFont exceeds the download limit")
            if offset and response.status_code == 206:
                content_range = response.headers.get("Content-Range", "")
                if not content_range.startswith(f"bytes {offset}-"):
                    raise MusicError("download_failed", "SoundFont mirror returned a bad range")
            if response.status_code == 200:
                offset = 0  # 服务端忽略 Range 时从头写，避免拼接两个完整文件。

            mode = "ab" if offset else "wb"
            with part.open(mode) as stream:
                async for chunk in response.aiter_bytes():
                    if not chunk:
                        continue
                    offset += len(chunk)
                    if offset > MAX_SOUNDFONT_BYTES:
                        raise MusicError("download_failed", "SoundFont exceeds the download limit")
                    await asyncio.to_thread(stream.write, chunk)
                    if progress is not None:
                        progress(entry.id, offset, entry.size_bytes)

        if entry.size_bytes is not None and offset != entry.size_bytes:
            # 保留短读文件供下一次 Range 续传；超长文件则无法恢复。
            if offset > entry.size_bytes:
                await asyncio.to_thread(part.unlink, missing_ok=True)
            raise MusicError("download_failed", "SoundFont download ended before completion")
        return await self._commit(part, destination, entry)

    async def _commit(self, part: Path, destination: Path, entry: CatalogEntry) -> Path:
        try:
            await asyncio.to_thread(_check_catalog_file, part, entry)
        except MusicError:
            await asyncio.to_thread(part.unlink, missing_ok=True)
            raise
        # 0644：只读 bind mount（如 Codex 沙箱 /opt/soundfonts）也能读取。
        await asyncio.to_thread(os.chmod, part, 0o644)
        await asyncio.to_thread(os.replace, part, destination)
        return destination
