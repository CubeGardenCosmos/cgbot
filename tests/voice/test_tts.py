from __future__ import annotations

from pathlib import Path

import pytest
from voice_fakes import Starter, make_wav

from cgbot.voice.errors import (
    VoiceInputError,
    VoicePathError,
    VoiceProtocolError,
    VoiceServiceError,
)
from cgbot.voice.paths import PathMapper, PathRule
from cgbot.voice.transport import Endpoint, JsonServiceClient
from cgbot.voice.tts import MAX_TEXT_CHARS, TtsClient


def _tts(host: str, port: int, mapper: PathMapper, language: str = "zh") -> TtsClient:
    transport = JsonServiceClient(
        "sovits", Endpoint(host=host, port=port), connect_timeout=2.0, max_connections=1
    )
    return TtsClient(transport, mapper, language=language, request_timeout=5.0)


def _reply(host_path: str, container_path: str) -> dict[str, object]:
    return {
        "status": "ok",
        "host_path": host_path,
        "container_path": container_path,
        "sampling_rate": 32000,
        "duration_sec": 1.5,
        "character": "klp",
        "elapsed_ms": 900,
        "device": "cuda:0",
    }


async def test_synthesize_reads_wav_via_host_path(start_tcp: Starter, tmp_path: Path) -> None:
    temp = tmp_path / "temp"
    temp.mkdir()
    make_wav(temp / "a.wav")
    mapper = PathMapper([PathRule(remote_prefix="/srv/qqbot/data/temp", local_prefix=str(temp))])
    running = await start_tcp(
        lambda req: _reply("/srv/qqbot/data/temp/a.wav", "/AstrBot/data/temp/a.wav")
    )
    assert running.host is not None and running.port is not None
    result = await _tts(running.host, running.port, mapper).synthesize("你好")

    assert result.wav == (temp / "a.wav").read_bytes()
    assert result.local_path == (temp / "a.wav").resolve()
    assert result.sampling_rate == 32000
    assert result.duration_sec == 1.5
    assert result.elapsed_ms == 900
    assert result.device == "cuda:0"
    assert running.daemon.requests == [{"action": "tts", "text": "你好", "language": "zh"}]


async def test_falls_back_to_container_path(start_tcp: Starter, tmp_path: Path) -> None:
    temp = tmp_path / "temp"
    temp.mkdir()
    make_wav(temp / "b.wav")
    mapper = PathMapper([PathRule(remote_prefix="/AstrBot/data/temp", local_prefix=str(temp))])
    running = await start_tcp(
        lambda req: _reply("/srv/qqbot/data/temp/b.wav", "/AstrBot/data/temp/b.wav")
    )
    assert running.host is not None and running.port is not None
    result = await _tts(running.host, running.port, mapper).synthesize("hi")
    assert result.local_path == (temp / "b.wav").resolve()


async def test_request_carries_only_text_and_language(start_tcp: Starter, tmp_path: Path) -> None:
    mapper = PathMapper([PathRule(remote_prefix="/srv", local_prefix=str(tmp_path))])
    running = await start_tcp(lambda req: _reply("/srv/x.wav", "/srv/x.wav"))
    assert running.host is not None and running.port is not None
    with pytest.raises(VoicePathError):  # the fake reply names a file that does not exist
        await _tts(running.host, running.port, mapper).synthesize("hi", language="en")
    assert running.daemon.requests == [{"action": "tts", "text": "hi", "language": "en"}]


async def test_service_error_is_raised(start_tcp: Starter, tmp_path: Path) -> None:
    mapper = PathMapper([PathRule(remote_prefix="/srv", local_prefix=str(tmp_path))])
    running = await start_tcp(lambda req: {"status": "error", "error": "model busy"})
    assert running.host is not None and running.port is not None
    with pytest.raises(VoiceServiceError) as info:
        await _tts(running.host, running.port, mapper).synthesize("hi")
    assert info.value.action == "tts"
    assert "model busy" in info.value.message


@pytest.mark.parametrize("text", ["", "   ", "x" * (MAX_TEXT_CHARS + 1)])
async def test_invalid_text_rejected_before_sending(
    start_tcp: Starter, tmp_path: Path, text: str
) -> None:
    mapper = PathMapper([PathRule(remote_prefix="/srv", local_prefix=str(tmp_path))])
    running = await start_tcp(lambda req: _reply("/srv/x.wav", "/srv/x.wav"))
    assert running.host is not None and running.port is not None
    with pytest.raises(VoiceInputError):
        await _tts(running.host, running.port, mapper).synthesize(text)
    assert running.daemon.connections == 0


@pytest.mark.parametrize(
    "reply",
    [
        {
            "status": "ok",
            "host_path": "/srv/x.wav",
            "container_path": "/srv/x.wav",
            "duration_sec": 1.0,
            "elapsed_ms": 1,
        },
        {
            "status": "ok",
            "host_path": "/srv/x.wav",
            "container_path": "/srv/x.wav",
            "sampling_rate": 1,
            "duration_sec": -1,
            "elapsed_ms": 1,
        },
        {
            "status": "ok",
            "host_path": "/srv/x.wav",
            "container_path": "/srv/x.wav",
            "sampling_rate": 1,
            "duration_sec": 1.0,
            "elapsed_ms": 1.5,
        },
        {"text": "no status"},
        {"status": "error"},
    ],
)
async def test_malformed_response_is_protocol_error(
    start_tcp: Starter, tmp_path: Path, reply: dict[str, object]
) -> None:
    mapper = PathMapper([PathRule(remote_prefix="/srv", local_prefix=str(tmp_path))])
    running = await start_tcp(lambda req: reply)
    assert running.host is not None and running.port is not None
    with pytest.raises(VoiceProtocolError):
        await _tts(running.host, running.port, mapper).synthesize("hi")


async def test_unmappable_output_path_raises_path_error(start_tcp: Starter, tmp_path: Path) -> None:
    mapper = PathMapper([PathRule(remote_prefix="/srv", local_prefix=str(tmp_path))])
    running = await start_tcp(lambda req: _reply("/other/x.wav", "/other/x.wav"))
    assert running.host is not None and running.port is not None
    with pytest.raises(VoicePathError):
        await _tts(running.host, running.port, mapper).synthesize("hi")


async def test_ping_returns_status(start_tcp: Starter, tmp_path: Path) -> None:
    mapper = PathMapper([PathRule(remote_prefix="/srv", local_prefix=str(tmp_path))])
    running = await start_tcp(lambda req: {"status": "ok", "device": "cuda:0"})
    assert running.host is not None and running.port is not None
    status = await _tts(running.host, running.port, mapper).ping()
    assert status["device"] == "cuda:0"
    assert running.daemon.requests == [{"action": "ping"}]
