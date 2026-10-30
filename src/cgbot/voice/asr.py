"""Client for the FunASR daemon (speech-to-text over the length-prefixed JSON protocol).

Only audio bytes are sent. The request never carries a file path, so a caller cannot make the
service read an arbitrary server-side file. Input must be a RIFF/WAVE container; conversion
from QQ's SILK/AMR formats happens in :mod:`cgbot.voice.media` before this client is called.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Final

from cgbot.voice.errors import VoiceInputError, VoiceProtocolError, VoiceServiceError
from cgbot.voice.transport import JsonServiceClient

ACTION_TRANSCRIBE: Final = "transcribe"
ACTION_PING: Final = "ping"
MAX_AUDIO_BYTES: Final = 36 * 1024 * 1024  # base64 grows by 4/3; keeps the frame under 50 MiB
_MAX_LANGUAGE_CHARS: Final = 16


@dataclass(frozen=True, slots=True)
class Transcript:
    text: str
    raw_text: str
    device: str
    elapsed_ms: int


def check_wav(data: bytes) -> None:
    """Reject anything that is not a plausible RIFF/WAVE payload within the size limit."""
    if not data:
        raise VoiceInputError("audio is empty")
    if len(data) > MAX_AUDIO_BYTES:
        raise VoiceInputError("audio exceeds the ASR size limit")
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise VoiceInputError("audio must be a RIFF/WAVE file")


class AsrClient:
    """High-level ASR calls on top of a :class:`JsonServiceClient`."""

    def __init__(
        self,
        transport: JsonServiceClient,
        *,
        language: str,
        request_timeout: float,
    ) -> None:
        if not language or len(language) > _MAX_LANGUAGE_CHARS:
            raise ValueError("language must be a short non-empty tag")
        if request_timeout <= 0:
            raise ValueError("request_timeout must be positive")
        self._transport = transport
        self._language = language
        self._timeout = request_timeout

    async def transcribe(self, wav: bytes, *, language: str | None = None) -> Transcript:
        """Transcribe one WAV payload. Raises a :class:`VoiceError` subclass on failure."""
        check_wav(wav)
        lang = language or self._language
        response = await self._transport.call(
            {
                "action": ACTION_TRANSCRIBE,
                "audio_b64": base64.b64encode(wav).decode("ascii"),
                "language": lang,
            },
            timeout=self._timeout,
        )
        _raise_on_error(ACTION_TRANSCRIBE, response)
        text = _str_field(response, "text")
        raw_text = _str_field(response, "raw_text", default=text)
        device = _str_field(response, "device", default="unknown")
        elapsed = response.get("elapsed_ms", 0)
        if isinstance(elapsed, bool) or not isinstance(elapsed, int) or elapsed < 0:
            raise VoiceProtocolError("elapsed_ms is not a non-negative integer")
        return Transcript(text=text, raw_text=raw_text, device=device, elapsed_ms=elapsed)

    async def ping(self) -> dict[str, object]:
        """Health probe. Returns the daemon's status object (device, model, CUDA info)."""
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


def _str_field(response: dict[str, object], key: str, default: str | None = None) -> str:
    value = response.get(key, default)
    if not isinstance(value, str):
        raise VoiceProtocolError(f"response field {key!r} is not a string")
    return value
