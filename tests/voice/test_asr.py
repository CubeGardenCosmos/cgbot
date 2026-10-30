from __future__ import annotations

import base64

import pytest
from voice_fakes import Starter, make_wav

from cgbot.voice.asr import MAX_AUDIO_BYTES, AsrClient, check_wav
from cgbot.voice.errors import VoiceInputError, VoiceProtocolError, VoiceServiceError
from cgbot.voice.transport import Endpoint, JsonServiceClient


def _asr(host: str, port: int, language: str = "auto") -> AsrClient:
    transport = JsonServiceClient(
        "funasr", Endpoint(host=host, port=port), connect_timeout=2.0, max_connections=1
    )
    return AsrClient(transport, language=language, request_timeout=5.0)


def _ok(req: dict[str, object]) -> dict[str, object]:
    return {
        "status": "ok",
        "text": "你好",
        "raw_text": "你 好",
        "device": "cuda:0",
        "elapsed_ms": 42,
    }


async def test_transcribe_sends_only_audio_and_language(start_tcp: Starter) -> None:
    wav = make_wav()
    running = await start_tcp(_ok)
    assert running.host is not None and running.port is not None
    client = _asr(running.host, running.port, language="zh")

    result = await client.transcribe(wav)

    assert result.text == "你好"
    assert result.raw_text == "你 好"
    assert result.device == "cuda:0"
    assert result.elapsed_ms == 42
    request = running.daemon.requests[0]
    # No path field: a caller can never make the service read a server-side file.
    assert set(request) == {"action", "audio_b64", "language"}
    assert request["action"] == "transcribe"
    assert request["language"] == "zh"
    assert base64.b64decode(str(request["audio_b64"])) == wav


async def test_per_call_language_overrides_default(start_tcp: Starter) -> None:
    running = await start_tcp(_ok)
    assert running.host is not None and running.port is not None
    await _asr(running.host, running.port, language="zh").transcribe(make_wav(), language="en")
    assert running.daemon.requests[0]["language"] == "en"


async def test_raw_text_defaults_to_text_when_absent(start_tcp: Starter) -> None:
    running = await start_tcp(lambda req: {"status": "ok", "text": "hi"})
    assert running.host is not None and running.port is not None
    result = await _asr(running.host, running.port).transcribe(make_wav())
    assert result.raw_text == "hi"
    assert result.device == "unknown"
    assert result.elapsed_ms == 0


@pytest.mark.parametrize("data", [b"", b"not a wav file at all", b"RIFF1234AVI "])
async def test_non_wav_input_rejected_before_sending(start_tcp: Starter, data: bytes) -> None:
    running = await start_tcp(_ok)
    assert running.host is not None and running.port is not None
    with pytest.raises(VoiceInputError):
        await _asr(running.host, running.port).transcribe(data)
    assert running.daemon.connections == 0


def test_check_wav_accepts_minimal_riff_wave_header() -> None:
    check_wav(b"RIFF" + b"\x00" * 4 + b"WAVE")


def test_check_wav_rejects_oversized_payload() -> None:
    oversized = b"RIFF" + b"\x00" * 4 + b"WAVE" + b"\x00" * MAX_AUDIO_BYTES
    with pytest.raises(VoiceInputError):
        check_wav(oversized)


async def test_service_error_surfaces_and_connection_stays_usable(start_tcp: Starter) -> None:
    replies: list[dict[str, object]] = [
        {"status": "error", "error": "bad audio"},
        {"status": "ok", "text": "ok", "raw_text": "ok", "device": "cpu", "elapsed_ms": 1},
    ]
    running = await start_tcp(lambda req: replies.pop(0))
    assert running.host is not None and running.port is not None
    client = _asr(running.host, running.port)
    with pytest.raises(VoiceServiceError) as info:
        await client.transcribe(make_wav())
    assert info.value.action == "transcribe"
    assert "bad audio" in info.value.message
    assert (await client.transcribe(make_wav())).text == "ok"
    assert running.daemon.connections == 1


@pytest.mark.parametrize(
    "reply",
    [
        {"status": "ok", "raw_text": "x", "device": "cpu", "elapsed_ms": 1},
        {"status": "ok", "text": 3, "elapsed_ms": 1},
        {"status": "ok", "text": "x", "elapsed_ms": -1},
        {"status": "ok", "text": "x", "elapsed_ms": True},
        {"text": "x"},
        {"status": "error"},
    ],
)
async def test_malformed_response_is_protocol_error(
    start_tcp: Starter, reply: dict[str, object]
) -> None:
    running = await start_tcp(lambda req: reply)
    assert running.host is not None and running.port is not None
    with pytest.raises(VoiceProtocolError):
        await _asr(running.host, running.port).transcribe(make_wav())


async def test_ping_returns_status_object(start_tcp: Starter) -> None:
    running = await start_tcp(
        lambda req: {"status": "ok", "device": "cuda:0", "model": "m", "cuda_available": True}
    )
    assert running.host is not None and running.port is not None
    status = await _asr(running.host, running.port).ping()
    assert status["device"] == "cuda:0"
    assert running.daemon.requests == [{"action": "ping"}]
