"""音色目录只读挂载与启动任务。离线。"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from music_fakes import music_config

from cgbot.coding.auth import HmacSandboxAuth
from cgbot.coding.sandbox import (
    MUSE_ENV,
    MUSE_HOME_MOUNT,
    MUSE_LIBRARY_MOUNT,
    MUSE_SAMPLES_MOUNT,
    SOUNDFONT_MOUNT,
    DockerSandboxProvider,
)
from cgbot.config.settings import MusicConfig
from cgbot.music.errors import MusicError
from cgbot.music.soundfont import SoundfontStore
from cgbot.music.startup import ensure_default_soundfont, prepare_soundfont_dir


async def _sandbox_spec(
    tmp_path: Path,
    soundfont_dir: Path | None = None,
    muse_dir: Path | None = None,
) -> tuple[list[str], str, dict[str, str]]:
    config = music_config(tmp_path)
    assert config.coding is not None
    provider = DockerSandboxProvider(
        config.coding,
        HmacSandboxAuth(config.coding.token_secret),
        soundfont_dir=soundfont_dir,
        muse_dir=muse_dir,
    )
    try:
        body = provider._desired_body("u1", "ws-token")
        env = dict(item.split("=", 1) for item in body["Env"])
        return list(body["HostConfig"]["Binds"]), provider._config_hash(body), env
    finally:
        await provider.close()


def _muse_layout(root: Path, *, omit: str | None = None) -> None:
    paths = (
        ("Muse Hub Instruments", True),
        ("MuseSampler/lib", True),
        ("home", True),
        ("machine-id", False),
    )
    for name, is_dir in paths:
        if name == omit:
            continue
        path = root / name
        if is_dir:
            path.mkdir(parents=True, exist_ok=True)
        else:
            path.write_text("test-machine-id", encoding="utf-8")


async def test_bind_added_read_only_when_dir_exists(tmp_path: Path) -> None:
    fonts = tmp_path / "fonts"
    fonts.mkdir()
    binds, _, _ = await _sandbox_spec(tmp_path, fonts)
    assert f"{fonts}:{SOUNDFONT_MOUNT}:ro" in binds
    assert SOUNDFONT_MOUNT == "/opt/soundfonts"
    assert len(binds) == 3
    assert all(not bind.endswith(":rw") for bind in binds if SOUNDFONT_MOUNT in bind)


async def test_no_bind_when_not_configured_or_missing(tmp_path: Path) -> None:
    unconfigured, plain_hash, _ = await _sandbox_spec(tmp_path)
    missing, _, _ = await _sandbox_spec(tmp_path, tmp_path / "absent")
    assert len(unconfigured) == 2
    assert missing == unconfigured
    assert not any(SOUNDFONT_MOUNT in bind for bind in unconfigured)
    fonts = tmp_path / "fonts"
    fonts.mkdir()
    _, mounted_hash, _ = await _sandbox_spec(tmp_path, fonts)
    assert mounted_hash != plain_hash  # 挂载变化会让空闲沙盒按配置哈希重建


async def test_relative_dir_becomes_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "rel").mkdir()
    monkeypatch.chdir(tmp_path)
    binds, _, _ = await _sandbox_spec(tmp_path, Path("rel"))
    assert f"{tmp_path / 'rel'}:{SOUNDFONT_MOUNT}:ro" in binds


async def test_muse_complete_layout_adds_read_only_binds_env_and_hash(tmp_path: Path) -> None:
    muse = tmp_path / "muse"
    _muse_layout(muse)
    _, plain_hash, plain_env = await _sandbox_spec(tmp_path)
    binds, muse_hash, env = await _sandbox_spec(tmp_path, muse_dir=muse)
    assert binds[2:] == [
        f"{muse / 'Muse Hub Instruments'}:{MUSE_SAMPLES_MOUNT}:ro",
        f"{muse / 'MuseSampler' / 'lib'}:{MUSE_LIBRARY_MOUNT}:ro",
        f"{muse / 'home'}:{MUSE_HOME_MOUNT}:ro",
        f"{muse / 'machine-id'}:/etc/machine-id:ro",
    ]
    assert {key: env[key] for key in dict(item.split("=", 1) for item in MUSE_ENV)} == {
        "MUSESAMPLER_LIB": "/opt/musesampler/lib/libMuseSamplerCoreLib.so",
        "MUSE_SAMPLES_DIR": "/opt/muse-sounds",
        "CGMUSIC_MUSE_HOME": "/opt/muse-home",
    }
    muse_keys = set(dict(item.split("=", 1) for item in MUSE_ENV))
    assert not set(plain_env) & muse_keys
    assert muse_hash != plain_hash  # 挂载变化会让空闲沙盒按配置哈希重建


async def test_muse_partial_layout_mounts_nothing_and_sets_no_env(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    muse = tmp_path / "muse"
    _muse_layout(muse, omit="home")
    with caplog.at_level(logging.INFO):
        binds, _, env = await _sandbox_spec(tmp_path, muse_dir=muse)
    assert len(binds) == 2
    assert not set(env) & set(dict(item.split("=", 1) for item in MUSE_ENV))
    assert str(muse / "home") in caplog.text
    assert caplog.text.count("Muse mounts disabled") == 1


async def test_muse_unset_or_missing_layout_is_inert(tmp_path: Path) -> None:
    _, plain_hash, _ = await _sandbox_spec(tmp_path)
    binds, missing_hash, env = await _sandbox_spec(tmp_path, muse_dir=tmp_path / "absent")
    assert len(binds) == 2
    assert missing_hash == plain_hash
    assert not set(env) & set(dict(item.split("=", 1) for item in MUSE_ENV))


async def test_muse_relative_dir_becomes_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _muse_layout(tmp_path / "muse")
    monkeypatch.chdir(tmp_path)
    binds, _, _ = await _sandbox_spec(tmp_path, muse_dir=Path("muse"))
    assert f"{tmp_path / 'muse' / 'home'}:{MUSE_HOME_MOUNT}:ro" in binds


# ---------------------------------------------------------------- startup


def _music(tmp_path: Path, **overrides: object) -> MusicConfig:
    values: dict[str, object] = {"enabled": True, "soundfont_dir": tmp_path / "sf"}
    values.update(overrides)
    return MusicConfig(**values)  # type: ignore[arg-type]


def test_prepare_creates_world_readable_dir(tmp_path: Path) -> None:
    config = _music(tmp_path)
    assert prepare_soundfont_dir(config) is True
    assert config.soundfont_dir.is_dir()
    assert config.soundfont_dir.stat().st_mode & 0o777 == 0o755
    assert prepare_soundfont_dir(config) is True  # 幂等


def test_prepare_is_inert_when_disabled(tmp_path: Path) -> None:
    config = _music(tmp_path, enabled=False)
    assert prepare_soundfont_dir(config) is False
    assert not config.soundfont_dir.exists()


def test_prepare_failure_is_soft(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert prepare_soundfont_dir(_music(tmp_path, soundfont_dir=blocker / "sub")) is False


class _Store:
    def __init__(self, path: Path | Exception) -> None:
        self.path = path
        self.calls = 0

    async def ensure(self) -> Path:
        self.calls += 1
        if isinstance(self.path, Exception):
            raise self.path
        return self.path


async def test_ensure_default_soundfont_calls_store(tmp_path: Path) -> None:
    store = _Store(tmp_path / "MuseScore_General.sf3")
    assert await ensure_default_soundfont(_music(tmp_path), store) is True  # type: ignore[arg-type]
    assert store.calls == 1


@pytest.mark.parametrize("overrides", [{"enabled": False}, {"auto_download_soundfont": False}])
async def test_ensure_skipped_when_off(tmp_path: Path, overrides: dict[str, object]) -> None:
    store = _Store(tmp_path / "x.sf3")
    assert await ensure_default_soundfont(_music(tmp_path, **overrides), store) is False  # type: ignore[arg-type]
    assert store.calls == 0


async def test_ensure_failure_is_logged_not_raised(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = _Store(MusicError("download_failed", "https://secret.example/font.sf3 broke"))
    with caplog.at_level(logging.WARNING):
        assert await ensure_default_soundfont(_music(tmp_path), store) is False  # type: ignore[arg-type]
    assert "default soundfont not ensured: error=MusicError" in caplog.text
    assert "secret.example" not in caplog.text


async def test_ensure_with_real_store_and_no_network_is_soft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(self: SoundfontStore, *args: object, **kwargs: object) -> Path:
        raise OSError("offline")

    monkeypatch.setattr(SoundfontStore, "ensure", boom)
    config = _music(tmp_path)
    assert await ensure_default_soundfont(config) is False
    assert config.soundfont_dir.is_dir()  # 构造 SoundfontStore 会创建目录
