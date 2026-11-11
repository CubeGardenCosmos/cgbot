from __future__ import annotations

from typing import Any

import pytest

from cgbot.config.settings import ConfigError
from cgbot.voice.config import (
    DEFAULT_ASR_PORT,
    DEFAULT_ASR_TIMEOUT,
    DEFAULT_TTS_PORT,
    DEFAULT_TTS_TIMEOUT,
    default_voice_config,
    load_voice_config,
)


def test_absent_table_yields_defaults() -> None:
    cfg = default_voice_config()
    assert cfg.ffmpeg == "ffmpeg"
    assert cfg.media_timeout == 30.0
    assert cfg.asr.endpoint.describe() == f"tcp:127.0.0.1:{DEFAULT_ASR_PORT}"
    assert cfg.asr.request_timeout == DEFAULT_ASR_TIMEOUT
    assert cfg.asr.connect_timeout == 5.0
    assert cfg.asr.max_connections == 1
    assert cfg.asr_language == "auto"
    assert cfg.tts.endpoint.describe() == f"tcp:127.0.0.1:{DEFAULT_TTS_PORT}"
    assert cfg.tts.request_timeout == DEFAULT_TTS_TIMEOUT
    assert cfg.tts_language == "zh"
    assert cfg.path_rules == ()
    assert load_voice_config({}) == cfg


def test_full_tcp_table_parses() -> None:
    cfg = load_voice_config(
        {
            "ffmpeg": " /usr/bin/ffmpeg ",
            "media_timeout_seconds": 12,
            "asr": {
                "host": "192.0.2.10",
                "port": 20095,
                "connect_timeout_seconds": 2,
                "request_timeout_seconds": 45.5,
                "max_connections": 3,
                "language": "zh",
            },
            "tts": {
                "port": 20096,
                "language": "zh",
                "path_rules": [{"remote": "/AstrBot/data/temp", "local": "/srv/qqbot/data/temp"}],
            },
        }
    )
    assert cfg.ffmpeg == "/usr/bin/ffmpeg"
    assert cfg.media_timeout == 12.0
    assert cfg.asr.endpoint.describe() == "tcp:192.0.2.10:20095"
    assert cfg.asr.max_connections == 3
    assert cfg.asr.request_timeout == 45.5
    assert cfg.asr_language == "zh"
    assert cfg.tts.endpoint.describe() == "tcp:127.0.0.1:20096"
    assert len(cfg.path_rules) == 1
    assert cfg.path_rules[0].remote_prefix == "/AstrBot/data/temp"
    assert cfg.path_rules[0].local_prefix == "/srv/qqbot/data/temp"


def test_unix_socket_endpoint_parses() -> None:
    cfg = load_voice_config({"asr": {"unix_path": "/srv/qqbot/data/funasr.sock"}})
    assert cfg.asr.endpoint.describe() == "unix:/srv/qqbot/data/funasr.sock"


@pytest.mark.parametrize(
    ("raw", "key"),
    [
        ({"surprise": 1}, "voice.surprise"),
        ({"asr": {"surprise": 1}}, "voice.asr.surprise"),
        ({"tts": {"surprise": 1}}, "voice.tts.surprise"),
        ({"asr": {"unix_path": "/tmp/a.sock", "host": "127.0.0.1"}}, "voice.asr"),
        ({"asr": {"unix_path": "relative.sock"}}, "voice.asr.unix_path"),
        ({"asr": {"host": "8.8.8.8"}}, "voice.asr.host"),
        ({"asr": {"port": 0}}, "voice.asr.host"),
        ({"asr": {"max_connections": 0}}, "voice.asr.max_connections"),
        ({"asr": {"max_connections": 9}}, "voice.asr.max_connections"),
        ({"asr": {"max_connections": True}}, "voice.asr.max_connections"),
        ({"asr": {"request_timeout_seconds": 0}}, "voice.asr.request_timeout_seconds"),
        ({"tts": {"request_timeout_seconds": 4000}}, "voice.tts.request_timeout_seconds"),
        ({"media_timeout_seconds": "slow"}, "voice.media_timeout_seconds"),
        ({"ffmpeg": "  "}, "voice.ffmpeg"),
        ({"asr": "not a table"}, "voice.asr"),
        ({"tts": {"path_rules": "nope"}}, "voice.tts.path_rules"),
        ({"tts": {"path_rules": [{"remote": "/a"}]}}, "voice.tts.path_rules[0]"),
        ({"tts": {"path_rules": [{"remote": "/a", "local": 3}]}}, "voice.tts.path_rules[0]"),
        ({"tts": {"path_rules": [{"remote": "rel", "local": "/tmp"}]}}, "voice.tts.path_rules[0]"),
        (
            {"tts": {"path_rules": [{"remote": "/a", "local": "/tmp", "extra": 1}]}},
            "voice.tts.path_rules[0]",
        ),
    ],
)
def test_invalid_config_raises_with_key(raw: dict[str, Any], key: str) -> None:
    with pytest.raises(ConfigError) as info:
        load_voice_config(raw)
    assert info.value.key == key
