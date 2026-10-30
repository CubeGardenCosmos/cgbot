from __future__ import annotations

import base64
import io
import sys
import time
import types
import wave
from pathlib import Path

import pytest
from voice_fakes import have_ffmpeg, make_wav

from cgbot.voice.errors import (
    MediaDecodeError,
    MediaDependencyError,
    MediaEncodeError,
    MediaTimeoutError,
)
from cgbot.voice.media import (
    ASR_SAMPLE_RATE,
    MAX_INPUT_BYTES,
    SILK_PCM_RATE,
    MediaConverter,
    PilkCodec,
    is_silk,
    pcm_to_wav,
)


def _script(tmp_path: Path, body: str) -> str:
    """A fake ``ffmpeg`` executable running a POSIX shell body."""
    path = tmp_path / "ffmpeg"
    path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def _wav_info(data: bytes) -> tuple[int, int, int]:
    with wave.open(io.BytesIO(data), "rb") as reader:
        return reader.getnchannels(), reader.getsampwidth(), reader.getframerate()


class FakeSilk:
    """Records calls; decode returns fixed PCM, encode returns a SILK-looking blob."""

    def __init__(self, *, pcm: bytes = b"\x01\x00" * 2400) -> None:
        self.pcm = pcm
        self.decode_calls: list[tuple[bytes, int]] = []
        self.encode_calls: list[tuple[bytes, int]] = []

    def decode(self, silk: bytes, sample_rate: int) -> bytes:
        self.decode_calls.append((silk, sample_rate))
        return self.pcm

    def encode(self, pcm: bytes, sample_rate: int) -> bytes:
        self.encode_calls.append((pcm, sample_rate))
        return b"#!SILK_V3" + b"\x00" * 8


def test_pcm_to_wav_sets_mono_16bit_rate() -> None:
    data = pcm_to_wav(b"\x00\x00" * 160, 16000)
    assert _wav_info(data) == (1, 2, 16000)


def test_is_silk_detects_qq_prefixed_and_plain_magic() -> None:
    assert is_silk(b"#!SILK_V3rest")
    assert is_silk(b"\x02#!SILK_V3rest")
    assert not is_silk(make_wav())
    assert not is_silk(b"")


async def test_silk_input_decoded_at_24k_then_resampled(tmp_path: Path) -> None:
    fake = FakeSilk(pcm=b"\x01\x00" * 480)
    record = tmp_path / "args.txt"
    ffmpeg = _script(
        tmp_path,
        f"cat > /dev/null; printf '%s ' \"$@\" > '{record}'; head -c 320 /dev/zero",
    )
    converter = MediaConverter(ffmpeg=ffmpeg, timeout=5, silk=fake)
    wav = await converter.to_asr_wav(b"#!SILK_V3" + b"\x00" * 16)
    assert _wav_info(wav) == (1, 2, ASR_SAMPLE_RATE)
    assert fake.decode_calls[0][1] == SILK_PCM_RATE
    args = record.read_text()
    assert "24000" in args and "pipe:0" in args


async def test_qq_prefixed_silk_is_handed_to_codec_as_is(tmp_path: Path) -> None:
    fake = FakeSilk()
    converter = MediaConverter(
        ffmpeg=_script(tmp_path, "cat > /dev/null; head -c 64 /dev/zero"), timeout=5, silk=fake
    )
    payload = b"\x02#!SILK_V3" + b"\x00" * 8
    await converter.to_asr_wav(payload)
    assert fake.decode_calls[0][0] == payload


async def test_non_silk_input_goes_straight_to_ffmpeg(tmp_path: Path) -> None:
    fake = FakeSilk()
    converter = MediaConverter(ffmpeg=_script(tmp_path, "cat"), timeout=5, silk=fake)
    wav = await converter.to_asr_wav(make_wav(seconds=0.05))
    assert fake.decode_calls == []
    assert _wav_info(wav) == (1, 2, ASR_SAMPLE_RATE)


async def test_wav_to_silk_encodes_pcm_at_24k(tmp_path: Path) -> None:
    fake = FakeSilk()
    converter = MediaConverter(
        ffmpeg=_script(tmp_path, "cat > /dev/null; head -c 960 /dev/zero"),
        timeout=5,
        silk=fake,
    )
    silk = await converter.wav_to_silk(make_wav())
    assert is_silk(silk)
    assert fake.encode_calls[0][1] == SILK_PCM_RATE
    uri = await converter.to_record_uri(make_wav())
    assert uri.startswith("base64://")
    assert base64.b64decode(uri.removeprefix("base64://")).startswith(b"#!SILK_V3")


async def test_missing_ffmpeg_binary_is_dependency_error(tmp_path: Path) -> None:
    converter = MediaConverter(ffmpeg=str(tmp_path / "no-such-ffmpeg"), timeout=5)
    with pytest.raises(MediaDependencyError):
        await converter.to_asr_wav(make_wav())


async def test_ffmpeg_deadline_is_media_timeout(tmp_path: Path) -> None:
    converter = MediaConverter(ffmpeg=_script(tmp_path, "sleep 5"), timeout=0.2)
    with pytest.raises(MediaTimeoutError):
        await converter.to_asr_wav(make_wav())


async def test_ffmpeg_nonzero_exit_is_decode_error(tmp_path: Path) -> None:
    converter = MediaConverter(ffmpeg=_script(tmp_path, "cat > /dev/null; exit 1"), timeout=5)
    with pytest.raises(MediaDecodeError):
        await converter.to_asr_wav(make_wav())


async def test_ffmpeg_empty_output_is_decode_error(tmp_path: Path) -> None:
    converter = MediaConverter(ffmpeg=_script(tmp_path, "cat > /dev/null"), timeout=5)
    with pytest.raises(MediaDecodeError):
        await converter.to_asr_wav(make_wav())


async def test_wav_to_silk_ffmpeg_failure_is_encode_error(tmp_path: Path) -> None:
    converter = MediaConverter(
        ffmpeg=_script(tmp_path, "cat > /dev/null; exit 3"), timeout=5, silk=FakeSilk()
    )
    with pytest.raises(MediaEncodeError):
        await converter.wav_to_silk(make_wav())


async def test_empty_and_oversized_input_rejected(tmp_path: Path) -> None:
    converter = MediaConverter(ffmpeg=_script(tmp_path, "cat"), timeout=5)
    with pytest.raises(MediaDecodeError):
        await converter.to_asr_wav(b"")
    with pytest.raises(MediaDecodeError):
        await converter.to_asr_wav(b"\x00" * (MAX_INPUT_BYTES + 1))


async def test_silk_codec_failure_is_decode_error(tmp_path: Path) -> None:
    class Broken(FakeSilk):
        def decode(self, silk: bytes, sample_rate: int) -> bytes:
            raise RuntimeError("corrupt stream")

    converter = MediaConverter(ffmpeg=_script(tmp_path, "cat"), timeout=5, silk=Broken())
    with pytest.raises(MediaDecodeError):
        await converter.to_asr_wav(b"#!SILK_V3" + b"\x00" * 8)


async def test_silk_codec_empty_output_is_decode_error(tmp_path: Path) -> None:
    class Empty(FakeSilk):
        def decode(self, silk: bytes, sample_rate: int) -> bytes:
            return b""

    converter = MediaConverter(ffmpeg=_script(tmp_path, "cat"), timeout=5, silk=Empty())
    with pytest.raises(MediaDecodeError):
        await converter.to_asr_wav(b"#!SILK_V3" + b"\x00" * 8)


async def test_silk_codec_deadline_is_media_timeout(tmp_path: Path) -> None:
    class Slow(FakeSilk):
        def decode(self, silk: bytes, sample_rate: int) -> bytes:
            time.sleep(0.3)
            return b"\x00\x00"

    converter = MediaConverter(ffmpeg=_script(tmp_path, "cat"), timeout=0.05, silk=Slow())
    with pytest.raises(MediaTimeoutError):
        await converter.to_asr_wav(b"#!SILK_V3" + b"\x00" * 8)


def test_converter_rejects_non_positive_timeout() -> None:
    with pytest.raises(ValueError):
        MediaConverter(ffmpeg="ffmpeg", timeout=0)


def test_pilk_codec_without_package_is_dependency_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "pilk", None)  # makes ``import pilk`` raise ImportError
    with pytest.raises(MediaDependencyError):
        PilkCodec()


def test_pilk_codec_uses_file_api_and_strips_qq_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, object] = {}

    def decode(src: str, dst: str, pcm_rate: int) -> None:
        calls["decode_src"] = Path(src).read_bytes()
        calls["decode_rate"] = pcm_rate
        Path(dst).write_bytes(b"\x01\x00")

    def encode(src: str, dst: str, pcm_rate: int, tencent: bool) -> None:
        calls["encode_rate"] = pcm_rate
        calls["tencent"] = tencent
        Path(dst).write_bytes(b"#!SILK_V3")

    fake = types.SimpleNamespace(decode=decode, encode=encode)
    monkeypatch.setitem(sys.modules, "pilk", fake)

    codec = PilkCodec()
    assert codec.decode(b"\x02#!SILK_V3abc", 24000) == b"\x01\x00"
    assert calls["decode_src"] == b"#!SILK_V3abc"
    assert calls["decode_rate"] == 24000
    assert codec.encode(b"\x00\x00", 24000) == b"#!SILK_V3"
    assert calls["encode_rate"] == 24000
    assert calls["tencent"] is True


@pytest.mark.skipif(not have_ffmpeg(), reason="real ffmpeg not installed")
async def test_real_ffmpeg_resamples_wav_to_16k_mono() -> None:
    converter = MediaConverter(ffmpeg="ffmpeg", timeout=20)
    wav = await converter.to_asr_wav(make_wav(seconds=0.5, rate=44100))
    assert _wav_info(wav) == (1, 2, ASR_SAMPLE_RATE)


@pytest.mark.skipif(not have_ffmpeg(), reason="real ffmpeg not installed")
async def test_real_ffmpeg_rejects_garbage() -> None:
    converter = MediaConverter(ffmpeg="ffmpeg", timeout=20)
    with pytest.raises(MediaDecodeError):
        await converter.to_asr_wav(b"definitely not audio" * 50)
