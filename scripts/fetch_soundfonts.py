"""下载、列出与校验 cgbot 的 SF2/SF3 音色库。

在仓库根目录运行::

    uv run python scripts/fetch_soundfonts.py --list
    uv run python scripts/fetch_soundfonts.py                       # 下载推荐音色
    uv run python scripts/fetch_soundfonts.py --preset all
    uv run python scripts/fetch_soundfonts.py --preset keys,fm,chamber   # 按标签分组
    uv run python scripts/fetch_soundfonts.py --preset musescore-general-lite,fluidr3mono-gm
    uv run python scripts/fetch_soundfonts.py --verify
    uv run python scripts/fetch_soundfonts.py --print-mount

下载、断点续传与校验全部由 cgbot.music.soundfont.SoundfontStore 提供；
本脚本只负责命令行参数、进度条与退出码。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

import httpx

from cgbot.music.errors import MusicError
from cgbot.music.soundfont import SoundfontInfo, SoundfontStore

DEFAULT_DIR = Path("./data/soundfonts")
SANDBOX_MOUNT = "/opt/soundfonts"
_BAR_WIDTH = 30
_MIB = 1024 * 1024


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fetch_soundfonts.py",
        description="下载、列出与校验 cgbot 的 SF2/SF3 音色库。",
    )
    parser.add_argument(
        "--dir", type=Path, default=DEFAULT_DIR, help="音色目录（默认 ./data/soundfonts）"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--list", action="store_true", help="列出目录内容（不下载）")
    mode.add_argument("--verify", action="store_true", help="重新校验已安装的文件（不下载）")
    mode.add_argument(
        "--print-mount",
        action="store_true",
        help=f"打印只读 docker 挂载参数（-v <绝对目录>:{SANDBOX_MOUNT}:ro）后退出",
    )
    parser.add_argument(
        "--preset",
        default=None,
        metavar="SPEC",
        help=(
            "逗号分隔的记号：recommended（默认）、all、标签（keys、piano、epiano、fm、"
            "orchestra、chamber、general、lite）或音色 id。"
            "下载时选择要下载的音色；--verify 未指定时校验全部已安装的目录条目"
        ),
    )
    return parser


def _expand_token(token: str, store: SoundfontStore) -> list[str]:
    """单个 --preset 记号 → 音色 id：all / recommended / 目录 id / 标签（keys、fm、chamber…）。"""
    catalog = store.catalog
    if token == "all":
        return [entry.id for entry in catalog]
    if token == "recommended":
        default = store.default_id()
        if default is None:
            raise ValueError("the catalog has no recommended SoundFont")
        return [default]
    if any(entry.id == token for entry in catalog):
        return [token]
    tagged = [entry.id for entry in catalog if token in entry.tags]
    if tagged:
        return tagged
    raise ValueError(f"unknown SoundFont id: {token}")


def _resolve_ids(spec: str, store: SoundfontStore) -> list[str]:
    requested = [part.strip() for part in spec.split(",") if part.strip()]
    if not requested:
        raise ValueError("--preset needs a preset name, a tag, or one or more SoundFont ids")
    return list(
        dict.fromkeys(sf_id for token in requested for sf_id in _expand_token(token, store))
    )


def _installed_catalog_ids(store: SoundfontStore) -> list[str]:
    catalog_ids = {entry.id for entry in store.catalog}
    return [info.id for info in store.list() if info.installed and info.id in catalog_ids]


def _print_list(infos: Sequence[SoundfontInfo]) -> None:
    print(f"{'id':<27}{'name':<24}{'size':>11}  {'licence':<56}{'tags':<30}installed")
    for info in infos:
        sf_id = info.id + ("*" if info.recommended else "")
        size = "unknown" if info.size_bytes is None else f"{info.size_bytes / _MIB:.1f} MiB"
        tags = ",".join(info.tags) or "-"
        installed = "yes" if info.installed else "no"
        licence = info.license if len(info.license) <= 56 else info.license[:55] + "…"
        print(f"{sf_id:<27}{info.name:<24}{size:>11}  {licence:<56}{tags:<30}{installed}")
    print("* = recommended default")


class _Progress:
    """写入 stderr 的单行进度条；由 SoundfontStore 的 progress 回调驱动。"""

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        self._open = False

    def __call__(self, sf_id: str, done: int, total: int | None) -> None:
        if total:
            fraction = min(done / total, 1.0)
            filled = round(_BAR_WIDTH * fraction)
            bar = "#" * filled + "-" * (_BAR_WIDTH - filled)
            line = f"\r{sf_id} [{bar}] {done / _MIB:.1f}/{total / _MIB:.1f} MiB {fraction:6.1%}"
        else:
            line = f"\r{sf_id} {done / _MIB:.1f} MiB"
        self._stream.write(line)
        self._stream.flush()
        self._open = True

    def finish(self) -> None:
        if self._open:
            self._stream.write("\n")
            self._stream.flush()
            self._open = False


def _describe(exc: Exception) -> str:
    if isinstance(exc, MusicError):
        return f"{exc.code}: {exc.message}"
    return str(exc)


async def _run(store: SoundfontStore, *, verify: bool, ids: Sequence[str]) -> int:
    failures = 0
    progress = _Progress(sys.stderr)
    for sf_id in ids:
        try:
            if verify:
                path = await store.verify(sf_id)
            else:
                path = await store.ensure(sf_id, progress=progress)
        except (MusicError, OSError) as exc:
            progress.finish()
            failures += 1
            print(f"FAIL {sf_id}: {_describe(exc)}", file=sys.stderr)
        else:
            progress.finish()
            print(f"OK   {sf_id}  {path}")
    return 1 if failures else 0


async def _main_async(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
    transport: httpx.AsyncBaseTransport | None,
) -> int:
    directory: Path = args.dir.expanduser().resolve()
    client = (
        httpx.AsyncClient(transport=transport, follow_redirects=True)
        if transport is not None
        else None
    )
    try:
        store = SoundfontStore(directory, client=client)
        if args.list:
            _print_list(store.list())
            return 0
        if args.verify and args.preset is None:
            ids = _installed_catalog_ids(store)
            if not ids:
                print(f"no installed catalog SoundFonts in {directory}", file=sys.stderr)
                return 1
        else:
            try:
                ids = _resolve_ids(args.preset or "recommended", store)
            except ValueError as exc:
                parser.error(str(exc))
        return await _run(store, verify=args.verify, ids=ids)
    finally:
        if client is not None:
            await client.aclose()


def main(
    argv: Sequence[str] | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    """命令行入口。``transport`` 仅供离线测试注入 httpx.MockTransport。"""
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.print_mount:
        print(f"-v {args.dir.expanduser().resolve()}:{SANDBOX_MOUNT}:ro")
        return 0
    return asyncio.run(_main_async(args, parser, transport))


if __name__ == "__main__":
    raise SystemExit(main())
