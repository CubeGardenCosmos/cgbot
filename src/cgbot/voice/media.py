"""Audio conversion between QQ voice formats and the WAV files the services use.

Pipeline:

* inbound (QQ record -> ASR): SILK is decoded by a :class:`SilkCodec` to raw PCM, then ffmpeg
  resamples to 16 kHz mono; AMR and other containers go straight through ffmpeg. The result is
  wrapped in a RIFF/WAVE header with the stdlib ``wave`` module.
* outbound (TTS WAV -> QQ record): ffmpeg produces 24 kHz mono PCM, which the codec encodes to
  SILK. The record is sent as ``base64://...``.

ffmpeg runs as an asyncio subprocess with pipes on both ends (no temp files for ffmpeg). The
SILK codec runs in a worker thread. SILK support needs the ``pilk`` package, which is NOT a
project dependency yet; without it, SILK paths raise :class:`MediaDependencyError` and the rest
of the pipeline still works.
"""

from __future__ import annotations

import asyncio
import base64
import importlib
import io
import tempfile
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Final, Protocol

from cgbot.voice.errors import (
    MediaDecodeError,
    MediaDependencyError,
    MediaEncodeError,
    MediaError,
    MediaTimeoutError,
)

ASR_SAMPLE_RATE: Final = 16000
SILK_PCM_RATE: Final = 24000
MAX_INPUT_BYTES: Final = 16 * 1024 * 1024
# Output cap passed to ffmpeg with -t: bounds memory of the PCM read back from the pipe.
MAX_AUDIO_SECONDS: Final = 300
_SILK_MAGIC: Final = b"#!SILK_V3"


class SilkCodec(Protocol):
    """Blocking SILK <-> PCM (s16le mono) codec. Implementations are called from a thread."""

    def decode(self, silk: bytes, sample_rate: int) -> bytes: ...

    def encode(self, pcm: bytes, sample_rate: int) -> bytes: ...


class PilkCodec:
    """SILK codec backed by the optional ``pilk`` package.

    The import is lazy so the module loads without it; a missing package raises
    :class:`MediaDependencyError` when the codec is constructed. The ``pilk`` calls follow its
    file-based API (``decode(in, out, pcm_rate=)`` / ``encode(in, out, pcm_rate=, tencent=)``),
    which is unverified in this repo because pilk is not installed.
    """

    def __init__(self) -> None:
        try:
            self._pilk = importlib.import_module("pilk")
        except ImportError as exc:
            raise MediaDependencyError("SILK support needs the 'pilk' package") from exc

    def decode(self, silk: bytes, sample_rate: int) -> bytes:
        # QQ prefixes SILK files with one 0x02 byte; the codec expects "#!SILK_V3" first.
        payload = silk[1:] if silk.startswith(b"\x02") else silk
        with tempfile.TemporaryDirectory(prefix="cgbot-silk-") as tmp:
            src = Path(tmp) / "in.silk"
            dst = Path(tmp) / "out.pcm"
            src.write_bytes(payload)
            self._pilk.decode(str(src), str(dst), pcm_rate=sample_rate)
            return dst.read_bytes()

    def encode(self, pcm: bytes, sample_rate: int) -> bytes:
        with tempfile.TemporaryDirectory(prefix="cgbot-silk-") as tmp:
            src = Path(tmp) / "in.pcm"
            dst = Path(tmp) / "out.silk"
            src.write_bytes(pcm)
            self._pilk.encode(str(src), str(dst), pcm_rate=sample_rate, tencent=True)
            return dst.read_bytes()


def pcm_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    """Wrap mono 16-bit PCM in a RIFF/WAVE container."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(sample_rate)
        out.writeframes(pcm)
    return buffer.getvalue()


def is_silk(data: bytes) -> bool:
    return data.startswith(_SILK_MAGIC) or data.startswith(b"\x02" + _SILK_MAGIC)


class MediaConverter:
    """Owns the ffmpeg binary, the per-call deadline, and the optional SILK codec."""

    def __init__(
        self,
        *,
        ffmpeg: str,
        timeout: float,
        silk: SilkCodec | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._ffmpeg = ffmpeg
        self._timeout = timeout
        self._silk = silk

    async def to_asr_wav(self, audio: bytes) -> bytes:
        """Convert any supported inbound record to 16 kHz mono WAV for the ASR service."""
        _check_input(audio)
        if is_silk(audio):
            pcm_in = await self._in_thread(
                self._codec().decode, audio, SILK_PCM_RATE, error=MediaDecodeError
            )
            pcm = await self._ffmpeg_run(
                ["-f", "s16le", "-ar", str(SILK_PCM_RATE), "-ac", "1", "-i", "pipe:0"],
                pcm_in,
                ASR_SAMPLE_RATE,
                MediaDecodeError,
            )
        else:
            pcm = await self._ffmpeg_run(["-i", "pipe:0"], audio, ASR_SAMPLE_RATE, MediaDecodeError)
        return pcm_to_wav(pcm, ASR_SAMPLE_RATE)

    async def wav_to_silk(self, wav: bytes) -> bytes:
        """Convert a WAV payload (e.g. TTS output) to SILK for QQ record sending."""
        _check_input(wav)
        pcm = await self._ffmpeg_run(["-i", "pipe:0"], wav, SILK_PCM_RATE, MediaEncodeError)
        return await self._in_thread(
            self._codec().encode, pcm, SILK_PCM_RATE, error=MediaEncodeError
        )

    async def to_record_uri(self, wav: bytes) -> str:
        """SILK record as a ``base64://`` URI. NapCat acceptance is unverified (see R4.3)."""
        silk = await self.wav_to_silk(wav)
        return "base64://" + base64.b64encode(silk).decode("ascii")

    def _codec(self) -> SilkCodec:
        if self._silk is None:
            self._silk = PilkCodec()
        return self._silk

    async def _in_thread(
        self,
        fn: Callable[[bytes, int], bytes],
        data: bytes,
        rate: int,
        *,
        error: type[MediaError],
    ) -> bytes:
        # The worker thread cannot be interrupted; on timeout its result is discarded.
        try:
            async with asyncio.timeout(self._timeout):
                result = await asyncio.to_thread(fn, data, rate)
        except TimeoutError as exc:
            raise MediaTimeoutError("SILK codec exceeded its deadline") from exc
        except MediaError:
            raise
        except Exception as exc:
            # Codec libraries raise their own exception types; surface one taxonomy error.
            raise error("SILK codec failed") from exc
        if not result:
            raise error("SILK codec produced no output")
        return result

    async def _ffmpeg_run(
        self,
        input_args: list[str],
        stdin: bytes,
        out_rate: int,
        error: type[MediaError],
    ) -> bytes:
        args = [
            "-hide_banner",
            "-loglevel",
            "error",
            *input_args,
            "-t",
            str(MAX_AUDIO_SECONDS),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(out_rate),
            "-f",
            "s16le",
            "pipe:1",
        ]
        return await self._exec(args, stdin, error)

    async def _exec(self, args: list[str], stdin: bytes, error: type[MediaError]) -> bytes:
        try:
            proc = await asyncio.create_subprocess_exec(
                self._ffmpeg,
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            raise MediaDependencyError(f"ffmpeg is not runnable: {self._ffmpeg}") from exc
        try:
            async with asyncio.timeout(self._timeout):
                out, _ = await proc.communicate(stdin)
        except TimeoutError as exc:
            raise MediaTimeoutError("ffmpeg exceeded its deadline") from exc
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        if proc.returncode != 0:
            raise error(f"ffmpeg exited with status {proc.returncode}")
        if not out:
            raise error("ffmpeg produced no output")
        return out


def _check_input(data: bytes) -> None:
    if not data:
        raise MediaDecodeError("audio is empty")
    if len(data) > MAX_INPUT_BYTES:
        raise MediaDecodeError("audio exceeds the input size limit")
