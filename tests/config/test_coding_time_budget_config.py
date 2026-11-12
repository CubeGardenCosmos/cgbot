"""M5:``[coding.time_budget]`` 配置的默认值、覆盖与严格校验(离线)。"""

from __future__ import annotations

from pathlib import Path

import pytest

from cgbot.coding.config import TimeBudgetConfig
from cgbot.config import ConfigError, load_config

SECRET_VALUE = "coding-token-value"

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
groups = [2001]
"""

CODING_TOML = """
[coding]
token_secret = "secret:coding_token"

[coding.http]
host = "0.0.0.0"
port = 8000
public_base_url = "http://cgbot:8000"

[coding.docker]
host = "http://caged-docker-proxy:2375"
sandbox_image = "cgbot-sandbox:latest"
network = "caged_internal"
host_data_dir = "/data/cgbot-data"
idle_seconds = 3600

[coding.codex]
model = "deepseek-v4.1-flash"
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
temp_dir = "data/bridge"
file_ttl_seconds = 86400
"""

SECRETS_TOML = (
    f'[secrets]\nprovider_api_key = "sk-test-provider"\ncoding_token = "{SECRET_VALUE}"\n'
)


@pytest.fixture
def secrets_file(tmp_path: Path) -> Path:
    path = tmp_path / "secrets.toml"
    path.write_text(SECRETS_TOML, encoding="utf-8")
    path.chmod(0o600)
    return path


def _load(tmp_path: Path, budget_block: str, secrets: Path):
    config_file = tmp_path / "config.toml"
    config_file.write_text(BASE_TOML + CODING_TOML + budget_block, encoding="utf-8")
    config_file.chmod(0o644)
    return load_config(config_file, secrets, env={})


def test_time_budget_defaults_apply_when_section_absent(tmp_path: Path, secrets_file: Path) -> None:
    cfg = _load(tmp_path, "", secrets_file)
    assert cfg.coding is not None
    assert cfg.coding.time_budget == TimeBudgetConfig()
    budget = cfg.coding.time_budget
    assert budget.soft_target_seconds == 120.0
    assert budget.cap_seconds == 600.0
    assert budget.grace_seconds == 120.0
    assert budget.notice_interval_seconds == 60.0
    assert budget.approach_window_seconds == 120.0
    assert budget.approach_interval_seconds == 30.0
    assert budget.max_important_minutes == 60


def test_time_budget_overrides_are_parsed(tmp_path: Path, secrets_file: Path) -> None:
    block = """
[coding.time_budget]
soft_target_seconds = 90
cap_seconds = 300
grace_seconds = 45.5
notice_interval_seconds = 40
approach_window_seconds = 60
approach_interval_seconds = 15
max_important_minutes = 30
"""
    cfg = _load(tmp_path, block, secrets_file)
    assert cfg.coding is not None
    assert cfg.coding.time_budget == TimeBudgetConfig(
        soft_target_seconds=90.0,
        cap_seconds=300.0,
        grace_seconds=45.5,
        notice_interval_seconds=40.0,
        approach_window_seconds=60.0,
        approach_interval_seconds=15.0,
        max_important_minutes=30,
    )


@pytest.mark.parametrize(
    ("line", "key"),
    [
        ("soft_target_seconds = 0", "coding.time_budget.soft_target_seconds"),
        ("cap_seconds = -1", "coding.time_budget.cap_seconds"),
        ("cap_seconds = 100", "coding.time_budget.cap_seconds"),  # 低于默认 soft_target 120
        ("grace_seconds = 0", "coding.time_budget.grace_seconds"),
        ('notice_interval_seconds = "60"', "coding.time_budget.notice_interval_seconds"),
        ("approach_window_seconds = 0", "coding.time_budget.approach_window_seconds"),
        ("approach_interval_seconds = 0", "coding.time_budget.approach_interval_seconds"),
        ("max_important_minutes = 0", "coding.time_budget.max_important_minutes"),
        ("max_important_minutes = 1.5", "coding.time_budget.max_important_minutes"),
        ("max_important_minutes = true", "coding.time_budget.max_important_minutes"),
        ("bogus = 1", "coding.time_budget.bogus"),
    ],
)
def test_invalid_time_budget_value_is_named(
    tmp_path: Path, secrets_file: Path, line: str, key: str
) -> None:
    with pytest.raises(ConfigError) as info:
        _load(tmp_path, f"\n[coding.time_budget]\n{line}\n", secrets_file)
    assert info.value.key == key


def test_cap_below_soft_target_is_rejected(tmp_path: Path, secrets_file: Path) -> None:
    block = "\n[coding.time_budget]\nsoft_target_seconds = 200\ncap_seconds = 150\n"
    with pytest.raises(ConfigError) as info:
        _load(tmp_path, block, secrets_file)
    assert info.value.key == "coding.time_budget.cap_seconds"
