"""``[provider.vision]`` loading: defaults off, validation, deploy template (offline)."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from cgbot.config import ConfigError, VisionConfig, load_config

REPO_ROOT = Path(__file__).resolve().parents[2]
BASE = """
[napcat]
port = 6199

[provider]
base_url = "https://up.example/v1"
model = "model-x"
context_window = 128000
api_key_ref = "provider_api_key"
"""


def _load(tmp_path: Path, extra: str = "") -> VisionConfig:
    secrets = tmp_path / "s.toml"
    secrets.write_text('[secrets]\nprovider_api_key = "x"\n', encoding="utf-8")
    secrets.chmod(0o600)
    cfg = tmp_path / "c.toml"
    cfg.write_text(BASE + extra, encoding="utf-8")
    return load_config(cfg, secrets, env={}).provider.vision


def test_vision_is_off_by_default_with_documented_limits(tmp_path: Path) -> None:
    assert _load(tmp_path) == VisionConfig(
        enabled=False,
        max_images_per_turn=4,
        recent_image_turns=3,
        max_edge=1536,
        retention_images=200,
        retention_days=30,
    )


def test_vision_values_are_read(tmp_path: Path) -> None:
    extra = (
        "\n[provider.vision]\nenabled = true\nmax_images_per_turn = 2\nrecent_image_turns = 1\n"
        "max_edge = 1024\nretention_images = 10\nretention_days = 7\n"
    )
    assert _load(tmp_path, extra) == VisionConfig(True, 2, 1, 1024, 10, 7)


@pytest.mark.parametrize(
    ("body", "key"),
    [
        ("enabled = 1", "provider.vision.enabled"),
        ("max_edge = 0", "provider.vision.max_edge"),
        ("recent_image_turns = -1", "provider.vision.recent_image_turns"),
        ("max_images_per_turn = 1.5", "provider.vision.max_images_per_turn"),
        ("retention_days = true", "provider.vision.retention_days"),
        ("edge = 3", "provider.vision.edge"),
    ],
)
def test_invalid_vision_value_is_named(tmp_path: Path, body: str, key: str) -> None:
    with pytest.raises(ConfigError) as info:
        _load(tmp_path, f"\n[provider.vision]\n{body}\n")
    assert info.value.key == key


def test_deploy_template_enables_vision_with_valid_keys() -> None:
    raw = tomllib.loads((REPO_ROOT / "deploy" / "config.toml").read_text(encoding="utf-8"))
    vision = raw["provider"]["vision"]
    assert vision["enabled"] is True
    assert set(vision) <= {
        "enabled",
        "max_images_per_turn",
        "recent_image_turns",
        "max_edge",
        "retention_images",
        "retention_days",
    }
