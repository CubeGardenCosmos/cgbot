"""``[coding]`` configuration (M5). Owned by lead; slices import, never edit.

TOML shape (parsed by ``cgbot.config.loader`` into :class:`CodingConfig`)::

    [coding]
    enabled = true
    token_secret = "secret:coding_token"      # HMAC key; per-sandbox tokens derive from it

    [coding.http]                             # gateway + bridge share one aiohttp server
    host = "0.0.0.0"
    port = 8000
    public_base_url = "http://cgbot:8000"     # how sandboxes AND NapCat reach this server

    [coding.docker]
    host = "http://caged-docker-proxy:2375"
    sandbox_image = "cgbot-sandbox:latest"
    network = "caged_internal"
    host_data_dir = "/data/cgbot-data"    # absolute HOST path, same path inside the bot
    egress_proxy = "http://caged-egress:7890"
    idle_seconds = 3600
    container_prefix = "cgbot-sbx-"

    [coding.codex]
    model = "deepseek-v4.1-flash"
    reasoning_effort = "medium"
    appserver_port = 8765
    context_window = 128000                   # optional; defaults to [provider].context_window

    [coding.concurrency]
    per_user = 4
    degraded = 1
    degrade_failures = 3
    degrade_window_seconds = 60
    recover_after_seconds = 3600

    [coding.bridge]
    max_file_bytes = 536870912
    chunk_bytes = 2097152
    temp_dir = "data/bridge"
    file_ttl_seconds = 86400

    [coding.imagegen]                         # optional; absent = image generation unavailable
    base_url = "https://..."
    api_key = "secret:imagegen_key"
    model = "..."
    mode = "..."
    timeout_seconds = 600
    proxy = ""
    max_concurrency = 2

Upstream (Responses) base URL and API key for the gateway are taken from ``[provider]``;
there is exactly one supplier.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from cgbot.config.settings import Secret


@dataclass(frozen=True, slots=True)
class HttpConfig:
    host: str
    port: int
    public_base_url: str


@dataclass(frozen=True, slots=True)
class DockerConfig:
    host: str
    sandbox_image: str
    network: str
    host_data_dir: Path
    egress_proxy: str
    idle_seconds: float
    container_prefix: str


@dataclass(frozen=True, slots=True)
class CodexConfig:
    model: str
    reasoning_effort: str
    appserver_port: int
    context_window: int  # defaults to [provider].context_window


@dataclass(frozen=True, slots=True)
class ConcurrencyConfig:
    per_user: int
    degraded: int
    degrade_failures: int
    degrade_window_seconds: float
    recover_after_seconds: float


@dataclass(frozen=True, slots=True)
class BridgeConfig:
    max_file_bytes: int
    chunk_bytes: int
    temp_dir: Path
    file_ttl_seconds: float


@dataclass(frozen=True, slots=True)
class ImageGenConfig:
    base_url: str
    api_key: Secret
    model: str
    mode: str
    timeout_seconds: float
    proxy: str
    max_concurrency: int


@dataclass(frozen=True, slots=True)
class CodingConfig:
    token_secret: Secret
    http: HttpConfig
    docker: DockerConfig
    codex: CodexConfig
    concurrency: ConcurrencyConfig
    bridge: BridgeConfig
    imagegen: ImageGenConfig | None
    upstream_api_key: Secret | None = None  # None = reuse [provider] api_key
