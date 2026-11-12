"""Stable, user-safe failures raised by the music feature."""

from __future__ import annotations


class MusicError(Exception):
    """A music operation failed; ``code`` is safe to expose to clients."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code
