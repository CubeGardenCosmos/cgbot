"""Voice subsystem: FunASR ASR client, SoVITS TTS client, and QQ audio conversion."""

from cgbot.voice.asr import AsrClient, Transcript
from cgbot.voice.config import VoiceConfig, load_voice_config
from cgbot.voice.errors import (
    MediaDecodeError,
    MediaDependencyError,
    MediaEncodeError,
    MediaError,
    MediaTimeoutError,
    VoiceConnectionError,
    VoiceError,
    VoiceInputError,
    VoicePathError,
    VoiceProtocolError,
    VoiceServiceError,
    VoiceTimeoutError,
)
from cgbot.voice.media import MediaConverter, PilkCodec, SilkCodec
from cgbot.voice.paths import PathMapper, PathRule
from cgbot.voice.transport import Endpoint, JsonServiceClient
from cgbot.voice.tts import Synthesis, TtsClient

__all__ = [
    "AsrClient",
    "Endpoint",
    "JsonServiceClient",
    "MediaConverter",
    "MediaDecodeError",
    "MediaDependencyError",
    "MediaEncodeError",
    "MediaError",
    "MediaTimeoutError",
    "PathMapper",
    "PathRule",
    "PilkCodec",
    "SilkCodec",
    "Synthesis",
    "Transcript",
    "TtsClient",
    "VoiceConfig",
    "VoiceConnectionError",
    "VoiceError",
    "VoiceInputError",
    "VoicePathError",
    "VoiceProtocolError",
    "VoiceServiceError",
    "VoiceTimeoutError",
    "load_voice_config",
]
