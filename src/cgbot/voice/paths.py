"""Map WAV paths reported by the SoVITS daemon to files readable by this process.

The daemon answers with ``host_path`` (its own filesystem) and ``container_path`` (the view the
AstrBot container used). Neither is readable here unless a rule maps it. Mapping is explicit
only: a path is never assumed readable because it looks absolute. Operators declare
``remote -> local`` prefixes; both same-host (``remote == local``) and shared-mount setups use
the same rule. Without a matching rule, resolution fails with :class:`VoicePathError`.

Safety: the reported path must be absolute and free of ``..`` segments; after mapping, its
real path (symlinks resolved) must stay under the rule's local root; and the file must be a
regular, readable RIFF/WAVE file no larger than :data:`MAX_WAV_BYTES`. The client never deletes
or modifies the file: the service owns it and currently never cleans it up.
"""

from __future__ import annotations

import os
import posixpath
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from cgbot.voice.errors import VoicePathError

MAX_WAV_BYTES: Final = 50 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class PathRule:
    """``remote_prefix`` as seen in service responses maps to ``local_prefix`` on this host."""

    remote_prefix: str
    local_prefix: str

    def __post_init__(self) -> None:
        for name, value in (("remote", self.remote_prefix), ("local", self.local_prefix)):
            if not value.startswith("/") or "\x00" in value:
                raise ValueError(f"path_rules {name} must be an absolute path")
            if _has_dotdot(value):
                raise ValueError(f"path_rules {name} must not contain '..'")
        if (
            posixpath.normpath(self.remote_prefix) == "/"
            or posixpath.normpath(self.local_prefix) == "/"
        ):
            raise ValueError("path_rules prefixes must not be the filesystem root")
        object.__setattr__(self, "remote_prefix", posixpath.normpath(self.remote_prefix))
        object.__setattr__(self, "local_prefix", posixpath.normpath(self.local_prefix))


def _has_dotdot(path: str) -> bool:
    return any(part == ".." for part in path.split("/"))


class PathMapper:
    """Resolves service-reported WAV paths to readable local files."""

    def __init__(self, rules: Sequence[PathRule] = ()) -> None:
        # Longest remote prefix wins, so nested mounts resolve to the most specific rule.
        self._rules = tuple(sorted(rules, key=lambda r: len(r.remote_prefix), reverse=True))

    def resolve_wav(self, *remote_paths: str) -> Path:
        """Return the first candidate that maps to a readable WAV; raise otherwise.

        Candidates are tried in the order given (callers pass ``host_path`` first).
        """
        reasons: list[str] = []
        for remote in remote_paths:
            try:
                local = self._map(remote)
            except VoicePathError as exc:
                reasons.append(str(exc))
                continue
            problem = _wav_problem(local)
            if problem is None:
                return local
            reasons.append(problem)
        detail = "; ".join(reasons) if reasons else "no candidate paths"
        raise VoicePathError(f"no readable local WAV: {detail}")

    def _map(self, remote: object) -> Path:
        if not isinstance(remote, str) or not remote:
            raise VoicePathError("service path is missing")
        if not remote.startswith("/") or "\x00" in remote or _has_dotdot(remote):
            raise VoicePathError("service path is not a safe absolute path")
        normal = posixpath.normpath(remote)
        for rule in self._rules:
            if normal == rule.remote_prefix or normal.startswith(rule.remote_prefix + "/"):
                relative = normal[len(rule.remote_prefix) :].lstrip("/")
                local = Path(rule.local_prefix) / relative if relative else Path(rule.local_prefix)
                root = os.path.realpath(rule.local_prefix)
                if os.path.commonpath([os.path.realpath(local), root]) != root:
                    raise VoicePathError("service path escapes its mapped root")
                return local
        raise VoicePathError(f"no path rule covers {normal}")


def _wav_problem(path: Path) -> str | None:
    """None when ``path`` is a readable RIFF/WAVE file within the size limit."""
    try:
        if not path.is_file():
            return f"{path} is not a regular file"
        size = path.stat().st_size
        if size > MAX_WAV_BYTES:
            return f"{path} exceeds the WAV size limit"
        with path.open("rb") as handle:
            header = handle.read(12)
    except OSError as exc:
        return f"{path} is not readable ({exc.strerror or type(exc).__name__})"
    if len(header) < 12 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
        return f"{path} is not a RIFF/WAVE file"
    return None
