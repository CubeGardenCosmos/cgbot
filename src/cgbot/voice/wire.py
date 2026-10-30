"""Length-prefixed JSON framing shared by the FunASR and SoVITS daemons.

Wire format (verified against ``funasr_service/server.py`` and ``sovits_service/server.py``):
a 4-byte big-endian unsigned length followed by that many bytes of UTF-8 JSON. Each request
gets exactly one response frame; the connection stays open for further requests. The servers
accept lengths in ``(0, 50 MiB]`` and drop the connection on anything else.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Final

from cgbot.voice.errors import VoiceConnectionError, VoiceInputError, VoiceProtocolError

MAX_FRAME_BYTES: Final = 50 * 1024 * 1024
_HEADER_BYTES: Final = 4


def encode_frame(payload: Mapping[str, object]) -> bytes:
    """Serialise one request into a complete frame. Oversized payloads are rejected here."""
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if not body or len(body) > MAX_FRAME_BYTES:
        raise VoiceInputError(f"request frame of {len(body)} bytes is outside (0, 50 MiB]")
    return len(body).to_bytes(_HEADER_BYTES, "big") + body


async def read_frame(reader: asyncio.StreamReader) -> bytes:
    """Read one complete frame body.

    ``readexactly`` assembles frames that arrive in arbitrary segments. EOF before any header
    byte means the peer closed without answering (connection error); EOF inside a header or
    body is a truncated frame (protocol error). A declared length outside ``(0, 50 MiB]`` is
    rejected without reading the body.
    """
    try:
        header = await reader.readexactly(_HEADER_BYTES)
    except asyncio.IncompleteReadError as exc:
        if not exc.partial:
            raise VoiceConnectionError("connection closed before a response") from exc
        raise VoiceProtocolError("truncated length header") from exc
    except OSError as exc:
        raise VoiceConnectionError(f"read failed: {exc.strerror or type(exc).__name__}") from exc

    length = int.from_bytes(header, "big")
    if length <= 0 or length > MAX_FRAME_BYTES:
        raise VoiceProtocolError(f"declared frame length {length} is outside (0, 50 MiB]")

    try:
        return await reader.readexactly(length)
    except asyncio.IncompleteReadError as exc:
        raise VoiceProtocolError(
            f"truncated frame: received {len(exc.partial)} of {length} bytes"
        ) from exc
    except OSError as exc:
        raise VoiceConnectionError(f"read failed: {exc.strerror or type(exc).__name__}") from exc


def decode_response(body: bytes) -> dict[str, object]:
    """Decode a complete frame body into a JSON object."""
    try:
        value: object = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VoiceProtocolError("response is not UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise VoiceProtocolError("response is not a JSON object")
    return value
