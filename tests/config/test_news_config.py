"""[news] config section: defaults, parsing, and validation (offline)."""

from __future__ import annotations

from datetime import time
from pathlib import Path

import pytest

from cgbot.config import Config, ConfigError, NewsConfig, load_config

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

SECRETS_TOML = '[secrets]\nprovider_api_key = "sk-test-news-config"\n'


def _write(tmp_path: Path, name: str, text: str, mode: int = 0o600) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)
    return path


def _load(tmp_path: Path, toml: str) -> Config:
    secrets = _write(tmp_path, "secrets.toml", SECRETS_TOML)
    config_file = _write(tmp_path, "config.toml", toml, mode=0o644)
    return load_config(config_file, secrets, env={})


def test_news_section_defaults_to_disabled(tmp_path: Path) -> None:
    cfg = _load(tmp_path, BASE_TOML)

    assert cfg.news == NewsConfig(
        enabled=False,
        output_dir=Path("/home/klp/news-auto-collect/output"),
        push_time=time(8, 0),
        give_up_hour=12,
        timezone="Asia/Shanghai",
    )


def test_news_section_parses_values_and_resolves_relative_output_dir(tmp_path: Path) -> None:
    toml = (
        BASE_TOML
        + """
[news]
enabled = true
output_dir = "news-out"
push_time = "07:30"
give_up_hour = 11
timezone = "UTC"
"""
    )

    cfg = _load(tmp_path, toml)

    assert cfg.news.enabled is True
    assert cfg.news.output_dir == tmp_path / "news-out"
    assert cfg.news.push_time == time(7, 30)
    assert cfg.news.give_up_hour == 11
    assert cfg.news.timezone == "UTC"


@pytest.mark.parametrize(
    ("line", "key"),
    [
        ('push_time = "8:00"', "push_time"),
        ('push_time = "25:00"', "push_time"),
        ('push_time = "08:60"', "push_time"),
        ("give_up_hour = 24", "give_up_hour"),
        ('timezone = "Mars/Olympus"', "timezone"),
    ],
)
def test_invalid_news_value_is_named(tmp_path: Path, line: str, key: str) -> None:
    with pytest.raises(ConfigError) as info:
        _load(tmp_path, BASE_TOML + f"\n[news]\n{line}\n")

    assert key in str(info.value)
