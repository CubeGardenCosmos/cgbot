"""Image blobs for chat history, kept per owning scope with bounded retention.

History rows keep only a ref (the file name); the bytes live here so session history never holds
base64. Every file sits under the directory of its owning session (``private:<user>`` or
``group:<gid>``) and is read back only through that same session id, so a ref stored by a group
session can never resolve in a private session or in another group, and vice versa.
Retention is per scope: files older than ``max_age_seconds`` and files beyond the newest
``max_files`` are deleted on every save and by :meth:`ImageStore.prune`.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import os
import re
import secrets
import time
from collections.abc import Callable, Sequence
from pathlib import Path

_PRIVATE_PREFIX = "private:"
_GROUP_PREFIX = "group:"
_SCOPE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_NAME = re.compile(r"(?P<ts>\d{10,})-[0-9a-f]{16}\.(?P<ext>jpg|png)")
_MIME = {"jpg": "image/jpeg", "png": "image/png"}
_STALE_TMP_SECONDS = 3600.0


def scope_dir(session_id: str) -> str:
    """Storage directory name of a session's images; raises ``ValueError`` for other sessions."""
    for prefix, label in ((_PRIVATE_PREFIX, "private"), (_GROUP_PREFIX, "group")):
        if session_id.startswith(prefix):
            ident = session_id[len(prefix) :]
            if _SCOPE_ID.fullmatch(ident):
                return f"{label}-{ident}"
    raise ValueError("session has no image scope")


class ImageStore:
    def __init__(
        self,
        root: Path,
        *,
        max_files: int,
        max_age_seconds: float,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if max_files < 1 or max_age_seconds <= 0:
            raise ValueError("retention limits must be positive")
        self._root = root
        self._max_files = max_files
        self._max_age = max_age_seconds
        self._clock = clock

    async def save(self, session_id: str, data: bytes, extension: str) -> str:
        """Store ``data`` under the session's scope and return its ref."""
        directory = self._root / scope_dir(session_id)
        if extension not in _MIME:
            raise ValueError("unsupported extension")
        return await asyncio.to_thread(self._save, directory, data, extension)

    async def load(self, session_id: str, refs: Sequence[str]) -> dict[str, str]:
        """``ref -> data URL`` for the refs that exist in this session's scope."""
        try:
            directory = self._root / scope_dir(session_id)
        except ValueError:
            return {}
        return await asyncio.to_thread(self._load, directory, list(refs))

    async def prune(self) -> int:
        """Apply retention to every scope. Returns the number of deleted files."""
        return await asyncio.to_thread(self._prune_all)

    def _save(self, directory: Path, data: bytes, extension: str) -> str:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        name = f"{int(self._clock())}-{secrets.token_hex(8)}.{extension}"
        tmp = directory / f".{name}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(tmp, directory / name)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                tmp.unlink()
            raise
        self._prune_scope(directory)
        return name

    @staticmethod
    def _load(directory: Path, refs: list[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for ref in refs:
            match = _NAME.fullmatch(ref)
            if match is None:
                continue
            path = directory / ref
            try:
                if path.is_symlink():
                    continue
                blob = path.read_bytes()
            except OSError:
                continue
            encoded = base64.b64encode(blob).decode("ascii")
            out[ref] = f"data:{_MIME[match['ext']]};base64,{encoded}"
        return out

    def _prune_scope(self, directory: Path) -> int:
        now = self._clock()
        files: list[tuple[int, str]] = []
        try:
            names = os.listdir(directory)
        except FileNotFoundError:
            return 0
        for name in names:
            match = _NAME.fullmatch(name)
            if match is not None:
                files.append((int(match["ts"]), name))
            elif name.startswith(".") and name.endswith(".tmp"):
                self._drop_stale_tmp(directory / name)
        files.sort()
        doomed = [name for ts, name in files if now - ts > self._max_age]
        survivors = [name for ts, name in files if now - ts <= self._max_age]
        doomed.extend(survivors[: max(0, len(survivors) - self._max_files)])
        removed = 0
        for name in doomed:
            try:
                (directory / name).unlink()
                removed += 1
            except FileNotFoundError:
                pass
        return removed

    @staticmethod
    def _drop_stale_tmp(path: Path) -> None:
        """Remove a temp file left by a crashed save; a fresh one may be a save in progress."""
        with contextlib.suppress(OSError):
            if time.time() - path.stat().st_mtime > _STALE_TMP_SECONDS:
                path.unlink()

    def _prune_all(self) -> int:
        try:
            scopes = [p for p in self._root.iterdir() if p.is_dir() and not p.is_symlink()]
        except FileNotFoundError:
            return 0
        return sum(self._prune_scope(scope) for scope in scopes)
