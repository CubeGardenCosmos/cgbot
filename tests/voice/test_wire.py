from __future__ import annotations

import asyncio
import json

import pytest

from cgbot.voice.errors import VoiceConnectionError, VoiceInputError, VoiceProtocolError
from cgbot.voice.wire import MAX_FRAME_BYTES, decode_response, encode_frame, read_frame


def _reader(*chunks: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    for chunk in chunks:
        reader.feed_data(chunk)
    reader.feed_eof()
    return reader


def test_encode_frame_prefixes_utf8_length() -> None:
    frame = encode_frame({"action": "ping", "text": "你好"})
    body = json.dumps({"action": "ping", "text": "你好"}, ensure_ascii=False, separators=(",", ":"))
    raw = body.encode("utf-8")
    assert frame == len(raw).to_bytes(4, "big") + raw


def test_encode_frame_rejects_oversized_payload() -> None:
    with pytest.raises(VoiceInputError):
        encode_frame({"audio_b64": "A" * MAX_FRAME_BYTES})


async def test_read_frame_round_trip() -> None:
    frame = encode_frame({"status": "ok"})
    body = await read_frame(_reader(frame))
    assert decode_response(body) == {"status": "ok"}


async def test_read_frame_assembles_byte_by_byte_segments() -> None:
    frame = encode_frame({"status": "ok", "text": "hello"})
    reader = asyncio.StreamReader()

    async def feed() -> None:
        for index in range(len(frame)):
            reader.feed_data(frame[index : index + 1])
            await asyncio.sleep(0)

    feeder = asyncio.create_task(feed())
    body = await read_frame(reader)
    await feeder
    assert decode_response(body) == {"status": "ok", "text": "hello"}


async def test_eof_before_any_header_is_connection_error() -> None:
    with pytest.raises(VoiceConnectionError):
        await read_frame(_reader())


async def test_truncated_header_is_protocol_error() -> None:
    with pytest.raises(VoiceProtocolError):
        await read_frame(_reader(b"\x00\x00"))


async def test_truncated_body_is_protocol_error() -> None:
    with pytest.raises(VoiceProtocolError):
        await read_frame(_reader((10).to_bytes(4, "big") + b"abc"))


@pytest.mark.parametrize("length", [0, MAX_FRAME_BYTES + 1, 0xFFFFFFFF])
async def test_out_of_range_declared_length_is_rejected_without_body(length: int) -> None:
    reader = _reader(length.to_bytes(4, "big"))
    with pytest.raises(VoiceProtocolError):
        await read_frame(reader)


def test_decode_response_rejects_non_utf8() -> None:
    with pytest.raises(VoiceProtocolError):
        decode_response(b"\xff\xfe")


def test_decode_response_rejects_non_object() -> None:
    with pytest.raises(VoiceProtocolError):
        decode_response(b"[1,2]")
    with pytest.raises(VoiceProtocolError):
        decode_response(b"not json")
