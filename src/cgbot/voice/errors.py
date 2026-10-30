"""Error taxonomy for voice clients and media conversion.

Every failure surfaced to callers is a :class:`VoiceError`. ``retryable`` is True only for
transient transport failures (connect/reset/timeout); protocol, input, and service errors are
deterministic for the same input and are not retried by the clients.
"""

from __future__ import annotations

from typing import ClassVar


class VoiceError(Exception):
    """Base class of every voice-subsystem failure."""

    retryable: ClassVar[bool] = False


class VoiceInputError(VoiceError):
    """Caller-supplied input is unusable (not WAV, empty text, over the frame limit).

    Raised before anything is sent to a service.
    """


class VoiceConnectionError(VoiceError):
    """Could not connect, or the connection was reset/closed before a response."""

    retryable = True


class VoiceTimeoutError(VoiceError):
    """Connect, queueing for a connection, or the request exceeded its deadline."""

    retryable = True


class VoiceProtocolError(VoiceError):
    """Malformed frame or response (bad length, truncated frame, non-JSON, wrong shape).

    The connection that produced it is discarded.
    """


class VoiceServiceError(VoiceError):
    """The service answered ``status != "ok"``. The connection stays usable."""

    def __init__(self, action: str, message: str) -> None:
        super().__init__(f"service action {action!r} failed: {message}")
        self.action = action
        self.message = message


class VoicePathError(VoiceError):
    """A service-reported WAV path cannot be mapped to a readable local WAV file."""


class MediaError(VoiceError):
    """Base class of audio conversion failures."""


class MediaDecodeError(MediaError):
    """Input is corrupt, empty, oversized, or in an unrecognised container."""


class MediaEncodeError(MediaError):
    """Encoding produced no usable output."""


class MediaDependencyError(MediaError):
    """A required external tool or optional codec package is not available."""


class MediaTimeoutError(MediaError):
    """An external conversion tool exceeded its deadline."""

    retryable = True
