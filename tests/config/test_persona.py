"""[persona] card loading, wake keyword precedence, owner injection, startup-only read."""

from __future__ import annotations

from pathlib import Path

import pytest

from cgbot.app import App
from cgbot.character import DEFAULT_CARD
from cgbot.config import ConfigError, load_config

BASE = """
[napcat]
port = 6199

[provider]
base_url = "https://up.example/v1"
model = "model-x"
context_window = 128000
api_key_ref = "provider_api_key"

[scope]
friends = [10001]
owner = 10001
"""
CARD = "---\nname: Bot\nwake_keywords:\n  - Bot\n---\nOwner is {owner_id}.\n"


def load(tmp_path: Path, extra: str = "") -> object:
    cfg = tmp_path / "config.toml"
    cfg.write_text(BASE + extra, encoding="utf-8")
    secrets = tmp_path / "s.toml"
    secrets.write_text('[secrets]\nprovider_api_key = "k"\n', encoding="utf-8")
    secrets.chmod(0o600)
    return load_config(cfg, secrets, env={})


def test_default_is_builtin_card(tmp_path: Path) -> None:
    assert load(tmp_path).persona == DEFAULT_CARD  # type: ignore[attr-defined]


def test_card_relative_to_config_dir_feeds_wake_keywords(tmp_path: Path) -> None:
    (tmp_path / "c.md").write_text(CARD, encoding="utf-8")
    cfg = load(tmp_path, '\n[persona]\ncard = "c.md"\n')
    assert cfg.persona.name == "Bot"  # type: ignore[attr-defined]
    assert cfg.group.wake_keywords == ("Bot",)  # type: ignore[attr-defined]


def test_group_wake_keywords_override_card(tmp_path: Path) -> None:
    (tmp_path / "c.md").write_text(CARD, encoding="utf-8")
    toml = '\n[persona]\ncard = "c.md"\n[group]\nwake_keywords = ["x"]\n'
    assert load(tmp_path, toml).group.wake_keywords == ("x",)  # type: ignore[attr-defined]


def test_missing_and_invalid_cards_are_config_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load(tmp_path, '\n[persona]\ncard = "nope.md"\n')
    (tmp_path / "bad.md").write_text("no frontmatter", encoding="utf-8")
    with pytest.raises(ConfigError):
        load(tmp_path, '\n[persona]\ncard = "bad.md"\n')


def test_unknown_persona_key_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as info:
        load(tmp_path, "\n[persona]\ntext = 'x'\n")
    assert info.value.key == "persona.text"


def test_shipped_cards_parse(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2] / "characters"
    for name in ("default.md", "neutral.md"):
        cfg = load(tmp_path, f'\n[persona]\ncard = "{root / name}"\n')
        assert cfg.persona.persona  # type: ignore[attr-defined]


async def test_owner_injected_and_persona_stable_without_rereading(tmp_path: Path) -> None:
    card = tmp_path / "c.md"
    card.write_text(CARD, encoding="utf-8")
    cfg = load(tmp_path, '\n[persona]\ncard = "c.md"\n')
    app = App(cfg, object())  # type: ignore[arg-type]
    try:
        first = await app._private_instructions("private:10001")
        assert "Owner is 10001." in first
        assert "{owner_id}" not in first
        card.write_text(CARD.replace("Owner is", "Changed"), encoding="utf-8")
        card.unlink()
        second = await app._private_instructions("private:10001")
        assert second == first
    finally:
        await app.stop()
