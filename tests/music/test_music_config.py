"""[music] 配置:默认值、默认音色目录、非法值。"""

from __future__ import annotations

from pathlib import Path

import pytest
from music_fakes import music_config

from cgbot.config import ConfigError


def test_music_defaults_off_and_dir_under_host_data(tmp_path: Path) -> None:
    config = music_config(tmp_path)
    music = config.music
    assert music.enabled is False
    assert music.auto_download_soundfont is True
    assert music.auto_render_files is True
    assert music.max_file_bytes == 8 * 1024 * 1024
    assert music.skill_ttl_seconds == 3600.0
    assert music.musescore_enabled is True
    assert music.soundfont_dir == tmp_path / "host-data" / "soundfonts"


def test_music_default_dir_without_coding_uses_data_dir(tmp_path: Path) -> None:
    config = music_config(tmp_path, "[music]\nenabled = true\n", coding=False)
    assert config.music.enabled is True
    assert config.music.soundfont_dir == tmp_path / "data" / "soundfonts"


def test_music_explicit_values_and_relative_dir(tmp_path: Path) -> None:
    config = music_config(
        tmp_path,
        '[music]\nenabled = true\nsoundfont_dir = "fonts"\nauto_download_soundfont = false\n'
        "auto_render_files = false\nmax_file_bytes = 1024\nskill_ttl_seconds = 60\n"
        "musescore_enabled = false\n",
    )
    music = config.music
    assert music.soundfont_dir == tmp_path / "fonts"
    assert not music.auto_download_soundfont
    assert not music.auto_render_files
    assert music.max_file_bytes == 1024
    assert music.skill_ttl_seconds == 60.0
    assert not music.musescore_enabled


@pytest.mark.parametrize(
    ("body", "key"),
    [
        ("enabled = 1", "music.enabled"),
        ("max_file_bytes = 0", "music.max_file_bytes"),
        ("skill_ttl_seconds = -1", "music.skill_ttl_seconds"),
        ("unknown = true", "music.unknown"),
        ('soundfont_dir = ""', "music.soundfont_dir"),
    ],
)
def test_music_invalid_values_name_the_key(tmp_path: Path, body: str, key: str) -> None:
    with pytest.raises(ConfigError) as caught:
        music_config(tmp_path, f"[music]\n{body}\n")
    assert caught.value.key == key


def test_deploy_template_enables_music() -> None:
    root = Path(__file__).resolve().parents[2]
    text = (root / "deploy" / "config.toml").read_text(encoding="utf-8")
    section = text.split("[music]", 1)[1].split("[coding]", 1)[0]
    assert "enabled = true" in section
