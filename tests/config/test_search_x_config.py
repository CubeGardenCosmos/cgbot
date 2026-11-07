"""[search.x] config: defaults, parsing, strict rejections (offline)."""

from __future__ import annotations

from pathlib import Path

import pytest

from cgbot.config import Config, ConfigError, load_config

BASE_TOML = """
[napcat]
port = 6199

[provider]
base_url = "https://up.example/v1"
model = "model-x"
context_window = 128000
api_key_ref = "provider_api_key"

[scope]
friends = [1001]
groups = []
"""

SECRETS = """
[secrets]
provider_api_key = "sk-test-SEARCH"
"""


def _load(tmp_path: Path, search: str = "") -> Config:
    config_file = tmp_path / "config.toml"
    secrets_file = tmp_path / "secrets.toml"
    data_dir = tmp_path / "data"
    config_file.write_text(
        BASE_TOML + f'\n[storage]\ndata_dir = "{data_dir}"\n' + search, encoding="utf-8"
    )
    secrets_file.write_text(SECRETS, encoding="utf-8")
    secrets_file.chmod(0o600)
    return load_config(config_file, secrets_file, env={})


def test_x_defaults(tmp_path: Path) -> None:
    x = _load(tmp_path).search.x
    assert x.enabled is False
    assert x.base_url == ""
    assert x.model == "grok-4.7"
    assert x.timeout_seconds == 90.0


def test_x_parsed(tmp_path: Path) -> None:
    x = _load(
        tmp_path,
        '\n[search.x]\nenabled = true\nbase_url = "https://relay.example/v1"\n'
        'model = "grok-other"\ntimeout_seconds = 30\n',
    ).search.x
    assert x.enabled is True
    assert x.base_url == "https://relay.example/v1"
    assert x.model == "grok-other"
    assert x.timeout_seconds == 30.0


def test_x_disabled_needs_no_base_url(tmp_path: Path) -> None:
    x = _load(tmp_path, "\n[search.x]\nenabled = false\n").search.x
    assert x.enabled is False


@pytest.mark.parametrize(
    "search",
    [
        "\n[search.x]\nenabled = true\n",
        '\n[search.x]\nenabled = true\nbase_url = "relay.example/v1"\n',
        '\n[search.x]\nenabled = true\nbase_url = "https://relay.example/v1"\n'
        "timeout_seconds = 0\n",
        '\n[search.x]\nenabled = true\nbase_url = "https://relay.example/v1"\n'
        'api_key_ref = "provider_api_key"\n',
    ],
)
def test_invalid_x_config_is_rejected(tmp_path: Path, search: str) -> None:
    with pytest.raises(ConfigError):
        _load(tmp_path, search)
