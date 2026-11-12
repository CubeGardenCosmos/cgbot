"""tests/music 的离线夹具:真实 TOML 加载的 Config,无网络、无 Docker。"""

from __future__ import annotations

from pathlib import Path

from cgbot.config import Config, load_config

_BASE = """
[napcat]
port = 6199

[provider]
base_url = "https://up.example/v1"
model = "model-x"
context_window = 128000
api_key_ref = "provider_api_key"

[scope]
friends = [1001]
groups = [2001]

[storage]
data_dir = "{data_dir}"
"""

_CODING = """
[coding]
token_secret = "secret:coding_token"

[coding.http]
host = "127.0.0.1"
port = 18080
public_base_url = "http://127.0.0.1:18080"

[coding.docker]
host = "http://127.0.0.1:2375"
sandbox_image = "cgbot-sandbox:test"
network = "cgbot_test"
host_data_dir = "{host_data}"
egress_proxy = ""
idle_seconds = 3600
container_prefix = "cgbot-sbx-"

[coding.codex]
model = "test-model"
reasoning_effort = "medium"
appserver_port = 8765

[coding.concurrency]
per_user = 4
degraded = 1
degrade_failures = 3
degrade_window_seconds = 60
recover_after_seconds = 3600

[coding.bridge]
max_file_bytes = 536870912
chunk_bytes = 2097152
temp_dir = "bridge"
file_ttl_seconds = 86400
"""


def music_config(tmp_path: Path, music_toml: str = "", *, coding: bool = True) -> Config:
    """``music_toml`` 是完整的 ``[music]`` 表文本(空串 = 省略整节)。"""
    secrets = tmp_path / "secrets.toml"
    secrets.write_text(
        '[secrets]\nprovider_api_key = "sk-test-MUSIC"\ncoding_token = "tok-test-MUSIC"\n',
        encoding="utf-8",
    )
    secrets.chmod(0o600)
    text = _BASE.format(data_dir=tmp_path / "data")
    if coding:
        text += _CODING.format(host_data=tmp_path / "host-data")
    text += "\n" + music_toml
    config_file = tmp_path / "config.toml"
    config_file.write_text(text, encoding="utf-8")
    return load_config(config_file, secrets, env={})
