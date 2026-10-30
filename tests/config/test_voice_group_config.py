"""Config keys for [voice] and [group] skill/idle limits load from TOML (offline)."""

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
groups = [2001]
"""

SECRETS_TOML = '[secrets]\nprovider_api_key = "sk-test-CFG"\n'


def load_with(tmp_path: Path, extra: str) -> Config:
    secrets = tmp_path / "secrets.toml"
    secrets.write_text(SECRETS_TOML, encoding="utf-8")
    secrets.chmod(0o600)
    config_file = tmp_path / "config.toml"
    config_file.write_text(BASE_TOML + extra, encoding="utf-8")
    config_file.chmod(0o644)
    return load_config(config_file, secrets, env={})


def test_voice_section_absent_means_voice_disabled(tmp_path: Path) -> None:
    assert load_with(tmp_path, "").voice is None


def test_voice_section_present_loads_voice_config(tmp_path: Path) -> None:
    assert load_with(tmp_path, "\n[voice]\n").voice is not None


def test_group_limits_default_when_absent(tmp_path: Path) -> None:
    group = load_with(tmp_path, "").group

    assert group.max_turns == 30
    assert group.idle_timeout_seconds == 600.0
    assert group.skill_topics == 8
    assert group.skill_memes == 10
    assert group.skill_rules == 10
    assert group.skill_members == 20
    assert group.skill_habits_per_member == 5


def test_group_limits_read_from_group_table(tmp_path: Path) -> None:
    extra = (
        "\n[group]\nmax_turns = 12\nidle_timeout_seconds = 900\n"
        "skill_topics = 3\nskill_habits_per_member = 2\n"
    )
    group = load_with(tmp_path, extra).group

    assert group.max_turns == 12
    assert group.idle_timeout_seconds == 900.0
    assert group.skill_topics == 3
    assert group.skill_habits_per_member == 2


@pytest.mark.parametrize("line", ["max_turns = 0", "skill_members = -1"])
def test_group_limits_reject_non_positive(tmp_path: Path, line: str) -> None:
    with pytest.raises(ConfigError):
        load_with(tmp_path, f"\n[group]\n{line}\n")
