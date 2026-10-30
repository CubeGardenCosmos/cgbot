"""Fake FunASR/SoVITS daemons speaking the length-prefixed JSON protocol, plus test helpers.

Each fake records every request and answers through a handler. A handler may return a dict
(sent as JSON), raw bytes (sent verbatim as the frame body, for malformed cases), or None
(close the connection without answering). ``delay`` postpones every reply, for timeout tests.
"""

from __future__ import annotations

import asyncio
import io
import json
import shutil
import struct
import subprocess
import wave
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from cgbot.voice.wire import MAX_FRAME_BYTES

Reply = dict[str, object] | bytes | None
Handler = Callable[[dict[str, object]], Reply]


@dataclass
class FakeDaemon:
    handler: Handler
    delay: float = 0.0
    requests: list[dict[str, object]] = field(default_factory=list)
    connections: int = 0
    lengths: list[int] = field(default_factory=list)

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        try:
            while True:
                try:
                    header = await reader.readexactly(4)
                except asyncio.IncompleteReadError:
                    return
                length = struct.unpack(">I", header)[0]
                self.lengths.append(length)
                if length <= 0 or length > MAX_FRAME_BYTES:
                    return  # the real daemons drop the connection on a bad length
                body = await reader.readexactly(length)
                request: dict[str, object] = json.loads(body.decode("utf-8"))
                self.requests.append(request)
                reply = self.handler(request)
                if self.delay:
                    await asyncio.sleep(self.delay)
                if reply is None:
                    return
                payload = reply if isinstance(reply, bytes) else json.dumps(reply).encode()
                writer.write(struct.pack(">I", len(payload)) + payload)
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            return
        finally:
            writer.close()


@dataclass(frozen=True, slots=True)
class Running:
    daemon: FakeDaemon
    host: str | None
    port: int | None
    unix_path: str | None


Starter = Callable[..., Awaitable[Running]]


def make_wav(path: Path | None = None, *, seconds: float = 0.1, rate: int = 16000) -> bytes:
    """Silent mono 16-bit WAV bytes, optionally also written to ``path``."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(b"\x00\x00" * int(seconds * rate))
    data = buffer.getvalue()
    if path is not None:
        path.write_bytes(data)
    return data


def have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def have_amr_encoder() -> bool:
    """Whether ffmpeg can encode AMR-NB, needed to build AMR fixtures."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        return False
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True, check=False
    )
    return "libopencore_amrnb" in result.stdout
