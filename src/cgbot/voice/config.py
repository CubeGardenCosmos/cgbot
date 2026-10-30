"""``[voice]`` configuration. Kept out of ``cgbot.config`` so the voice slice owns its keys.

The lead wires :func:`load_voice_config` into the main loader, passing the ``voice`` table
(or ``None`` when absent). Unknown keys and out-of-range values raise
:class:`cgbot.config.settings.ConfigError` with the dotted key path, like the main loader.

TOML shape::

    [voice]
    ffmpeg = "ffmpeg"                  # binary used for audio conversion
    media_timeout_seconds = 30

    [voice.asr]                        # FunASR daemon
    host = "127.0.0.1"                 # or: unix_path = "/home/klp/qqbot/data/funasr.sock"
    port = 10095
    connect_timeout_seconds = 5
    request_timeout_seconds = 60
    max_connections = 1
    language = "auto"

    [voice.tts]                        # SoVITS daemon
    host = "127.0.0.1"                 # or: unix_path = "/home/klp/qqbot/data/sovits.sock"
    port = 10096
    connect_timeout_seconds = 5
    request_timeout_seconds = 180
    max_connections = 1
    language = "zh"
    path_rules = [{ remote = "/AstrBot/data", local = "/home/klp/qqbot/data" }]
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from cgbot.config.settings import ConfigError
from cgbot.voice.paths import PathRule
from cgbot.voice.transport import Endpoint

DEFAULT_FFMPEG: Final = "ffmpeg"
DEFAULT_MEDIA_TIMEOUT: Final = 30.0
DEFAULT_CONNECT_TIMEOUT: Final = 5.0
DEFAULT_ASR_TIMEOUT: Final = 60.0
# Unverified estimate: measured synthesis runs 1.6-3.3x realtime, so a long sentence can take
# minutes. Tune against real measurements (R4.2 open decision).
DEFAULT_TTS_TIMEOUT: Final = 180.0
DEFAULT_MAX_CONNECTIONS: Final = 1
DEFAULT_ASR_PORT: Final = 10095
DEFAULT_TTS_PORT: Final = 10096
_LOCAL_HOST: Final = "127.0.0.1"


@dataclass(frozen=True, slots=True)
class ServiceConfig:
    endpoint: Endpoint
    connect_timeout: float
    request_timeout: float
    max_connections: int


@dataclass(frozen=True, slots=True)
class VoiceConfig:
    ffmpeg: str
    media_timeout: float
    asr: ServiceConfig
    asr_language: str
    tts: ServiceConfig
    tts_language: str
    path_rules: tuple[PathRule, ...] = field(default_factory=tuple)


def default_voice_config() -> VoiceConfig:
    """Configuration used when the ``[voice]`` table is absent."""
    return load_voice_config(None)


def load_voice_config(raw: Mapping[str, object] | None) -> VoiceConfig:
    """Validate the ``voice`` TOML table. ``None`` yields the defaults."""
    table = _Table("voice", dict(raw or {}))
    ffmpeg = table.text_or("ffmpeg", DEFAULT_FFMPEG)
    media_timeout = table.number_or("media_timeout_seconds", DEFAULT_MEDIA_TIMEOUT)
    asr_table = table.sub("asr")
    asr = _service("voice.asr", asr_table, DEFAULT_ASR_PORT, DEFAULT_ASR_TIMEOUT)
    asr_language = asr_table.text_or("language", "auto")
    asr_table.close()
    tts_table = table.sub("tts")
    tts = _service("voice.tts", tts_table, DEFAULT_TTS_PORT, DEFAULT_TTS_TIMEOUT)
    tts_language = tts_table.text_or("language", "zh")
    rules = _path_rules(tts_table)
    tts_table.close()
    table.close()
    return VoiceConfig(
        ffmpeg=ffmpeg,
        media_timeout=media_timeout,
        asr=asr,
        asr_language=asr_language,
        tts=tts,
        tts_language=tts_language,
        path_rules=rules,
    )


def _service(key: str, t: _Table, default_port: int, default_timeout: float) -> ServiceConfig:
    endpoint = _endpoint(key, t, default_port)
    connect = t.number_or("connect_timeout_seconds", DEFAULT_CONNECT_TIMEOUT)
    request = t.number_or("request_timeout_seconds", default_timeout)
    max_conn = t.int_or("max_connections", DEFAULT_MAX_CONNECTIONS)
    if not 1 <= max_conn <= 8:
        raise t.error("max_connections", "须在 1..8 之间")
    return ServiceConfig(
        endpoint=endpoint,
        connect_timeout=connect,
        request_timeout=request,
        max_connections=max_conn,
    )


def _endpoint(key: str, t: _Table, default_port: int) -> Endpoint:
    if t.has("unix_path"):
        if t.has("host") or t.has("port"):
            raise ConfigError(f"{key}", "unix_path 与 host/port 只能二选一")
        path = t.text("unix_path")
        try:
            return Endpoint(unix_path=path)
        except ValueError as exc:
            raise t.error("unix_path", str(exc)) from exc
    host = t.text_or("host", _LOCAL_HOST)
    port = t.int_or("port", default_port)
    try:
        return Endpoint(host=host, port=port)
    except ValueError as exc:
        raise t.error("host", str(exc)) from exc


def _path_rules(t: _Table) -> tuple[PathRule, ...]:
    raw = t.value_or("path_rules", [])
    if not isinstance(raw, list):
        raise t.error("path_rules", "须为数组")
    rules: list[PathRule] = []
    for index, item in enumerate(raw):
        key = f"voice.tts.path_rules[{index}]"
        if not isinstance(item, dict) or set(item) != {"remote", "local"}:
            raise ConfigError(key, '须为 { remote = "...", local = "..." }')
        remote, local = item["remote"], item["local"]
        if not isinstance(remote, str) or not isinstance(local, str):
            raise ConfigError(key, "remote 与 local 须为字符串")
        try:
            rules.append(PathRule(remote_prefix=remote, local_prefix=local))
        except ValueError as exc:
            raise ConfigError(key, str(exc)) from exc
    return tuple(rules)


class _Table:
    """Reads one TOML table, recording consumed keys so :meth:`close` rejects unknown ones."""

    def __init__(self, prefix: str, values: dict[str, object]) -> None:
        self._prefix = prefix
        self._values = values
        self._seen: set[str] = set()

    def error(self, key: str, message: str) -> ConfigError:
        return ConfigError(f"{self._prefix}.{key}", message)

    def has(self, key: str) -> bool:
        return key in self._values

    def value_or(self, key: str, default: object) -> object:
        self._seen.add(key)
        return self._values.get(key, default)

    def text_or(self, key: str, default: str) -> str:
        value = self.value_or(key, default)
        if not isinstance(value, str) or not value.strip():
            raise self.error(key, "须为非空字符串")
        return value.strip()

    def text(self, key: str) -> str:
        value = self.value_or(key, None)
        if not isinstance(value, str) or not value.strip():
            raise self.error(key, "须为非空字符串")
        return value.strip()

    def int_or(self, key: str, default: int) -> int:
        value = self.value_or(key, default)
        if isinstance(value, bool) or not isinstance(value, int):
            raise self.error(key, "须为整数")
        return value

    def number_or(self, key: str, default: float) -> float:
        value = self.value_or(key, default)
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise self.error(key, "须为数字")
        if not 0 < float(value) <= 3600:
            raise self.error(key, "须在 (0, 3600] 秒之间")
        return float(value)

    def sub(self, key: str) -> _Table:
        value = self.value_or(key, {})
        if not isinstance(value, dict):
            raise self.error(key, "须为表")
        return _Table(f"{self._prefix}.{key}", dict(value))

    def close(self) -> None:
        extra = sorted(set(self._values) - self._seen)
        if extra:
            raise self.error(extra[0], "未知配置键")
