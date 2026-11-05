"""Inbound images stored in each sandbox's workspace inbox (download via ``cgbot.vision.fetch``)."""

from __future__ import annotations

import contextlib
import errno
import os
import re
import stat
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

from cgbot.coding.contracts import workspace_dir
from cgbot.vision.fetch import Fetch, ImageError, default_fetch, fetch_image, sniff_extension

MAX_STAGED_FILES = 50
_USER_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_STAGED_NAME_RE = re.compile(r"\d{8}-\d{6}-[1-9]\d*\.(?:png|jpg|webp|gif)")
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_NEW_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC


class StageError(ImageError):
    """Public failure with a stable code; messages never contain remote URLs."""


@contextlib.contextmanager
def _inbox_fd(workspace: Path) -> Iterator[int]:
    # Walk from the filesystem root without resolve(): resolving first would trust symlinks.
    path = workspace.absolute() / "inbox"
    if ".." in path.parts:
        raise StageError("unsafe_path")
    fd = os.open(path.anchor, _DIR_FLAGS)
    try:
        for component in path.parts[1:]:
            created = False
            try:
                os.mkdir(component, mode=0o755, dir_fd=fd)
                created = True
            except FileExistsError:
                pass
            next_fd = os.open(component, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = next_fd
            if created:
                os.fchmod(fd, 0o755)
        yield fd
    finally:
        os.close(fd)


def _prune(fd: int) -> None:
    files: list[tuple[int, str]] = []
    for name in os.listdir(fd):
        if not _STAGED_NAME_RE.fullmatch(name):
            continue
        try:
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if stat.S_ISREG(info.st_mode):
            files.append((info.st_mtime_ns, name))
    files.sort()
    for _, name in files[: max(0, len(files) - MAX_STAGED_FILES)]:
        # unlink is relative to the pinned inbox and never follows symlinks.
        try:
            os.unlink(name, dir_fd=fd)
        except FileNotFoundError:
            pass


class ImageStager:
    """Stage magic-sniffed images; fetch and clock are injectable for offline callers."""

    def __init__(
        self,
        host_data_dir: Path,
        *,
        fetch: Fetch = default_fetch,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._host_data_dir = host_data_dir
        self._fetch = fetch
        self._clock = clock

    async def stage(self, user_id: str, url: str) -> str:
        if not _USER_ID_RE.fullmatch(user_id):
            raise StageError("invalid_user_id")
        try:
            data = await fetch_image(url, self._fetch)
            extension = sniff_extension(data)
        except ImageError as exc:
            raise StageError(exc.code) from None
        stamp = datetime.fromtimestamp(self._clock(), UTC).strftime("%Y%m%d-%H%M%S")
        try:
            with _inbox_fd(workspace_dir(self._host_data_dir, user_id)) as fd:
                number = 1
                while True:
                    name = f"{stamp}-{number}.{extension}"
                    try:
                        image_fd = os.open(name, _NEW_FLAGS, 0o644, dir_fd=fd)
                    except FileExistsError:
                        number += 1
                        continue
                    break
                try:
                    with os.fdopen(image_fd, "wb") as image:
                        os.fchmod(image.fileno(), 0o644)
                        image.write(data)
                except BaseException:
                    os.unlink(name, dir_fd=fd)
                    raise
                _prune(fd)
        except OSError as exc:
            code = "unsafe_path" if exc.errno in {errno.ELOOP, errno.ENOTDIR} else "storage_failed"
            raise StageError(code) from None
        return f"/workspace/inbox/{name}"
