"""Client for the SoVITS daemon (text-to-speech over the length-prefixed JSON protocol).

The voice character is fixed by the daemon, so requests carry only text and language. The
service writes its WAV to a directory it never cleans up; this client reads the file through
an explicit :class:`PathMapper` and never deletes or modifies it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from cgbot.voice.errors import (
    VoiceInputError,
    VoicePathError,
    VoiceProtocolError,
    VoiceServiceError,
)
from cgbot.voice.paths import PathMapper
from cgbot.voice.transport import JsonServiceClient

ACTION_TTS: Final = "tts"
ACTION_PING: Final = "ping"
# Client-side guard (推论): bounds how long one synthesis can run. Not taken from the server.
MAX_TEXT_CHARS: Final = 500
_MAX_LANGUAGE_CHARS: Final = 16


@dataclass(frozen=True, slots=True)
class Synthesis:
    wav: bytes
    local_path: Path
    sampling_rate: float
    duration_sec: float
    elapsed_ms: int
    device: str


class TtsClient:
    """High-level synthesis calls on top of a :class:`JsonServiceClient`."""

    def __init__(
        self,
        transport: JsonServiceClient,
        paths: PathMapper,
        *,
        language: str,
        request_timeout: float,
    ) -> None:
        if not language or len(language) > _MAX_LANGUAGE_CHARS:
            raise ValueError("language must be a short non-empty tag")
        if request_timeout <= 0:
            raise ValueError("request_timeout must be positive")
        self._transport = transport
        self._paths = paths
        self._language = language
        self._timeout = request_timeout

    async def synthesize(self, text: str, *, language: str | None = None) -> Synthesis:
        """Synthesise ``text`` and return the WAV bytes read from the mapped local file."""
        stripped = text.strip()
        if not stripped:
            raise VoiceInputError("text is empty")
        if len(stripped) > MAX_TEXT_CHARS:
            raise VoiceInputError(f"text exceeds {MAX_TEXT_CHARS} characters")
        response = await self._transport.call(
            {"action": ACTION_TTS, "text": stripped, "language": language or self._language},
            timeout=self._timeout,
        )
        _raise_on_error(ACTION_TTS, response)
        host_path = response.get("host_path")
        container_path = response.get("container_path")
        sampling_rate = _number(response, "sampling_rate")
        duration_sec = _number(response, "duration_sec")
        elapsed_ms = _non_negative_int(response, "elapsed_ms")
        device = _text(response, "device")
        local = self._paths.resolve_wav(
            *(p for p in (host_path, container_path) if isinstance(p, str))
        )
        try:
            wav = local.read_bytes()
        except OSError as exc:
            raise VoicePathError(f"could not read {local.name}") from exc
        return Synthesis(
            wav=wav,
            local_path=local,
            sampling_rate=sampling_rate,
            duration_sec=duration_sec,
            elapsed_ms=elapsed_ms,
            device=device,
        )

    async def ping(self) -> dict[str, object]:
        """Health probe. Returns the daemon's status object."""
        response = await self._transport.call({"action": ACTION_PING}, timeout=self._timeout)
        _raise_on_error(ACTION_PING, response)
        return response


def _raise_on_error(action: str, response: dict[str, object]) -> None:
    if response.get("status") == "ok":
        return
    message = response.get("error")
    if response.get("status") != "error" or not isinstance(message, str):
        raise VoiceProtocolError(f"{action} response has no valid status")
    raise VoiceServiceError(action, message)


def _number(response: dict[str, object], key: str) -> float:
    value = response.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        raise VoiceProtocolError(f"response field {key!r} is not a non-negative number")
    return float(value)


def _non_negative_int(response: dict[str, object], key: str) -> int:
    value = response.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise VoiceProtocolError(f"response field {key!r} is not a non-negative integer")
    return value


def _text(response: dict[str, object], key: str) -> str:
    value = response.get(key, "unknown")
    if not isinstance(value, str):
        raise VoiceProtocolError(f"response field {key!r} is not a string")
    return value
