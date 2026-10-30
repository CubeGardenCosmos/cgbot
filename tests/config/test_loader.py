"""R1.1-U01/U02: config loading, validation, secrets resolution, and redaction (offline)."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from cgbot.config import Config, ConfigError, Secret, load_config

REPO_ROOT = Path(__file__).resolve().parents[2]
SECRET_VALUE = "sk-test-SECRET-VALUE"

BASE_TOML = """
[napcat]
port = 6199

[provider]
base_url = "https://up.example/v1"
model = "model-x"
context_window = 128000
api_key_ref = "provider_api_key"

[scope]
friends = [1001, "1002"]
groups = [2001]
"""

SECRETS_TOML = f'[secrets]\nprovider_api_key = "{SECRET_VALUE}"\n'


def write(tmp_path: Path, name: str, text: str, mode: int = 0o600) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)
    return path


def edit(base: str, old: str, new: str) -> str:
    assert old in base, old
    return base.replace(old, new, 1)


@pytest.fixture
def secrets_file(tmp_path: Path) -> Path:
    return write(tmp_path, "secrets.toml", SECRETS_TOML)


def load_with(
    tmp_path: Path, toml: str, secrets: Path | None, env: dict[str, str] | None = None
) -> Config:
    config_file = write(tmp_path, "config.toml", toml, mode=0o644)
    return load_config(config_file, secrets, env={} if env is None else env)


# --- valid load -------------------------------------------------------------------------------


def test_valid_load_applies_defaults_and_reads_values(tmp_path: Path, secrets_file: Path) -> None:
    cfg = load_with(tmp_path, BASE_TOML, secrets_file)

    assert isinstance(cfg, Config)
    assert cfg.napcat.host == "127.0.0.1"
    assert cfg.napcat.port == 6199
    assert cfg.napcat.path == "/ws"
    assert cfg.napcat.token is None
    assert cfg.scope.owner is None
    assert cfg.scope.friends == frozenset({"1001", "1002"})
    assert cfg.scope.groups == frozenset({"2001"})
    assert cfg.provider.base_url == "https://up.example/v1"
    assert cfg.provider.model == "model-x"
    assert cfg.provider.context_window == 128000
    assert cfg.provider.api_key.reveal() == SECRET_VALUE
    assert cfg.provider.reasoning.summary == "low"
    assert cfg.provider.reasoning.private == "high"
    assert cfg.provider.reasoning.planning == "max"
    assert cfg.provider.reasoning.group == "low"
    assert cfg.group.wake_keywords == ("大肥鱼",)
    assert cfg.group.mute_keywords == ()
    assert cfg.storage.data_dir == tmp_path / "data"
    assert cfg.logging.level == "INFO"


def test_full_example_file_loads(tmp_path: Path) -> None:
    example = REPO_ROOT / "config.example.toml"
    secrets = write(tmp_path, "s.toml", '[secrets]\nprovider_api_key = "x-example"\n')

    cfg = load_config(example, secrets, env={})

    assert cfg.napcat.port == 6199
    assert cfg.scope.friends == frozenset()
    assert cfg.provider.api_key.reveal() == "x-example"
    assert cfg.storage.data_dir == example.parent / "data"


def test_values_are_normalised(tmp_path: Path, secrets_file: Path) -> None:
    toml = edit(BASE_TOML, "port = 6199", 'port = 6199\npath = "/custom"\nhost = "0.0.0.0"')
    toml = edit(toml, 'model = "model-x"', 'model = "  model-x  "')
    toml += (
        '\n[logging]\nlevel = "debug"\n'
        '\n[group]\nwake_keywords = ["a", "a", " b "]\nmute_keywords = ["x"]\n'
        '\n[storage]\ndata_dir = "/abs/dir"\n'
    )

    cfg = load_with(tmp_path, toml, secrets_file)

    assert cfg.provider.model == "model-x"
    assert cfg.logging.level == "DEBUG"
    assert cfg.napcat.path == "/custom"
    assert cfg.napcat.host == "0.0.0.0"
    assert cfg.group.wake_keywords == ("a", "b")
    assert cfg.group.mute_keywords == ("x",)
    assert cfg.storage.data_dir == Path("/abs/dir")


def test_relative_data_dir_is_relative_to_config_file(tmp_path: Path, secrets_file: Path) -> None:
    toml = BASE_TOML + '\n[storage]\ndata_dir = "runtime"\n'
    cfg = load_with(tmp_path, toml, secrets_file)
    assert cfg.storage.data_dir == tmp_path / "runtime"


def test_storage_lays_out_one_file_per_domain(tmp_path: Path, secrets_file: Path) -> None:
    toml = BASE_TOML + '\n[storage]\ndata_dir = "runtime"\n'
    cfg = load_with(tmp_path, toml, secrets_file)
    data = tmp_path / "runtime"
    assert cfg.storage.profile == data / "profile.sqlite"
    assert cfg.storage.sessions == data / "sessions.sqlite"
    assert cfg.storage.scheduler == data / "scheduler.sqlite"
    assert cfg.storage.for_module("memory") == data / "memory.sqlite"
    assert len({cfg.storage.profile, cfg.storage.sessions, cfg.storage.scheduler}) == 3
    for bad in ("", ".", "..", "a/b", "a\\b"):
        with pytest.raises(ValueError):
            cfg.storage.for_module(bad)


def test_owner_is_optional_and_parsed(tmp_path: Path, secrets_file: Path) -> None:
    toml = edit(BASE_TOML, "friends = [1001", 'owner = "3003"\nfriends = [1001')
    cfg = load_with(tmp_path, toml, secrets_file)
    assert cfg.scope.owner == "3003"


def test_token_ref_resolves_secret(tmp_path: Path) -> None:
    toml = edit(BASE_TOML, "port = 6199", 'port = 6199\ntoken_ref = "napcat_token"')
    secrets = write(
        tmp_path,
        "s.toml",
        f'[secrets]\nprovider_api_key = "{SECRET_VALUE}"\nnapcat_token = "tok-123"\n',
    )
    cfg = load_with(tmp_path, toml, secrets)
    assert cfg.napcat.token is not None
    assert cfg.napcat.token.reveal() == "tok-123"


# --- missing / invalid keys name the key ------------------------------------------------------


@pytest.mark.parametrize(
    ("old", "key"),
    [
        ("port = 6199\n", "napcat.port"),
        ("[napcat]\nport = 6199\n", "napcat"),
        ('base_url = "https://up.example/v1"\n', "provider.base_url"),
        ('model = "model-x"\n', "provider.model"),
        ("context_window = 128000\n", "provider.context_window"),
        ('api_key_ref = "provider_api_key"\n', "provider.api_key_ref"),
    ],
)
def test_missing_required_key_is_named(
    tmp_path: Path, secrets_file: Path, old: str, key: str
) -> None:
    toml = edit(BASE_TOML, old, "")
    with pytest.raises(ConfigError) as info:
        load_with(tmp_path, toml, secrets_file)
    assert info.value.key == key
    assert key in str(info.value)


def test_missing_provider_section_is_named(tmp_path: Path, secrets_file: Path) -> None:
    toml = BASE_TOML.split("[provider]")[0] + "[scope]\nfriends = []\n"
    with pytest.raises(ConfigError) as info:
        load_with(tmp_path, toml, secrets_file)
    assert info.value.key == "provider"


@pytest.mark.parametrize(
    ("old", "new", "key"),
    [
        ("port = 6199", 'port = "6199"', "napcat.port"),
        ("port = 6199", "port = 70000", "napcat.port"),
        ("port = 6199", "port = true", "napcat.port"),
        ("port = 6199", 'port = 6199\npath = "ws"', "napcat.path"),
        ('friends = [1001, "1002"]', 'friends = [1001, "abc"]', "scope.friends[1]"),
        ('friends = [1001, "1002"]', "friends = [0]", "scope.friends[0]"),
        ('friends = [1001, "1002"]', 'friends = "1001"', "scope.friends"),
        ("groups = [2001]", "groups = [-5]", "scope.groups[0]"),
        ('base_url = "https://up.example/v1"', 'base_url = "ftp://x"', "provider.base_url"),
        ('model = "model-x"', 'model = ""', "provider.model"),
        ("context_window = 128000", "context_window = 0", "provider.context_window"),
        ("context_window = 128000", "context_window = -1", "provider.context_window"),
        ("context_window = 128000", "context_window = true", "provider.context_window"),
        ("context_window = 128000", 'context_window = "128000"', "provider.context_window"),
        ("context_window = 128000", "context_window = 1.5", "provider.context_window"),
        ("[provider]", "[provider]\nreasoning = 3", "provider.reasoning"),
    ],
)
def test_invalid_value_is_named(
    tmp_path: Path, secrets_file: Path, old: str, new: str, key: str
) -> None:
    toml = edit(BASE_TOML, old, new)
    with pytest.raises(ConfigError) as info:
        load_with(tmp_path, toml, secrets_file)
    assert info.value.key == key
    assert key in str(info.value)


@pytest.mark.parametrize(
    ("suffix", "key"),
    [
        ('\n[group]\nwake_keywords = [""]\n', "group.wake_keywords[0]"),
        ('\n[logging]\nlevel = "VERBOSE"\n', "logging.level"),
        ('\n[storage]\ndata_dir = ""\n', "storage.data_dir"),
    ],
)
def test_invalid_value_in_trailing_section_is_named(
    tmp_path: Path, secrets_file: Path, suffix: str, key: str
) -> None:
    with pytest.raises(ConfigError) as info:
        load_with(tmp_path, BASE_TOML + suffix, secrets_file)
    assert info.value.key == key


# --- unknown keys -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("old", "new", "key"),
    [
        ("[napcat]", "bogus = 1\n[napcat]", "bogus"),
        ("port = 6199", "port = 6199\nprot = 1", "napcat.prot"),
        ("friends = [1001", "friend = [1001]\nfriends = [1001", "scope.friend"),
        (
            'api_key_ref = "provider_api_key"',
            'api_key_ref = "provider_api_key"\n[provider.reasoning]\nsumary = "low"',
            "provider.reasoning.sumary",
        ),
    ],
)
def test_unknown_key_is_rejected(
    tmp_path: Path, secrets_file: Path, old: str, new: str, key: str
) -> None:
    toml = edit(BASE_TOML, old, new)
    with pytest.raises(ConfigError) as info:
        load_with(tmp_path, toml, secrets_file)
    assert info.value.key == key


def test_unknown_section_is_rejected(tmp_path: Path, secrets_file: Path) -> None:
    with pytest.raises(ConfigError) as info:
        load_with(tmp_path, BASE_TOML + "\n[webui]\nenabled = true\n", secrets_file)
    assert info.value.key == "webui"


def test_unknown_key_in_secrets_file_is_rejected(tmp_path: Path) -> None:
    secrets = write(tmp_path, "s.toml", SECRETS_TOML + "\n[other]\nx = 1\n")
    with pytest.raises(ConfigError) as info:
        load_with(tmp_path, BASE_TOML, secrets)
    assert info.value.key == "other"


# --- file-level errors ------------------------------------------------------------------------


def test_missing_config_file_names_path(tmp_path: Path, secrets_file: Path) -> None:
    missing = tmp_path / "nope.toml"
    with pytest.raises(ConfigError) as info:
        load_config(missing, secrets_file, env={})
    assert info.value.key == str(missing)


def test_invalid_toml_names_path(tmp_path: Path, secrets_file: Path) -> None:
    with pytest.raises(ConfigError) as info:
        load_with(tmp_path, "[napcat\nport = ", secrets_file)
    assert info.value.key == str(tmp_path / "config.toml")


# --- secrets ----------------------------------------------------------------------------------


def test_secret_from_file(tmp_path: Path, secrets_file: Path) -> None:
    cfg = load_with(tmp_path, BASE_TOML, secrets_file)
    assert cfg.provider.api_key.reveal() == SECRET_VALUE


def test_env_overrides_secrets_file(tmp_path: Path, secrets_file: Path) -> None:
    cfg = load_with(
        tmp_path, BASE_TOML, secrets_file, env={"CGBOT_SECRET_PROVIDER_API_KEY": "sk-from-env"}
    )
    assert cfg.provider.api_key.reveal() == "sk-from-env"


def test_secret_from_env_without_secrets_file(tmp_path: Path) -> None:
    cfg = load_with(
        tmp_path, BASE_TOML, None, env={"CGBOT_SECRET_PROVIDER_API_KEY": "  sk-env-only  "}
    )
    assert cfg.provider.api_key.reveal() == "sk-env-only"


def test_secrets_path_taken_from_env_variable(tmp_path: Path) -> None:
    secrets = write(tmp_path, "from-env.toml", SECRETS_TOML)
    config_file = write(tmp_path, "config.toml", BASE_TOML)
    cfg = load_config(config_file, env={"CGBOT_SECRETS_FILE": str(secrets)})
    assert cfg.provider.api_key.reveal() == SECRET_VALUE


def test_explicit_secrets_path_beats_env_variable(tmp_path: Path) -> None:
    explicit = write(tmp_path, "explicit.toml", '[secrets]\nprovider_api_key = "sk-explicit"\n')
    config_file = write(tmp_path, "config.toml", BASE_TOML)
    cfg = load_config(
        config_file,
        explicit,
        env={"CGBOT_SECRETS_FILE": str(tmp_path / "does-not-exist.toml")},
    )
    assert cfg.provider.api_key.reveal() == "sk-explicit"


def test_missing_secret_names_ref_and_env_name(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as info:
        load_with(tmp_path, BASE_TOML, None, env={})
    assert info.value.key == "provider.api_key_ref"
    assert "provider_api_key" in str(info.value)
    assert "CGBOT_SECRET_PROVIDER_API_KEY" in str(info.value)


def test_empty_env_secret_is_rejected(tmp_path: Path, secrets_file: Path) -> None:
    with pytest.raises(ConfigError) as info:
        load_with(tmp_path, BASE_TOML, secrets_file, env={"CGBOT_SECRET_PROVIDER_API_KEY": "  "})
    assert info.value.key == "provider.api_key_ref"


def test_token_ref_missing_secret_names_token_ref(tmp_path: Path, secrets_file: Path) -> None:
    toml = edit(BASE_TOML, "port = 6199", 'port = 6199\ntoken_ref = "napcat_token"')
    with pytest.raises(ConfigError) as info:
        load_with(tmp_path, toml, secrets_file)
    assert info.value.key == "napcat.token_ref"


def test_invalid_secret_name_in_ref_is_rejected(tmp_path: Path, secrets_file: Path) -> None:
    toml = edit(BASE_TOML, 'api_key_ref = "provider_api_key"', 'api_key_ref = "Bad-Name"')
    with pytest.raises(ConfigError) as info:
        load_with(tmp_path, toml, secrets_file)
    assert info.value.key == "provider.api_key_ref"


@pytest.mark.parametrize(
    ("content", "key"),
    [
        ('[secrets]\nBad-Name = "x"\n', "secrets.Bad-Name"),
        ("[secrets]\nprovider_api_key = 1\n", "secrets.provider_api_key"),
        ('[secrets]\nprovider_api_key = ""\n', "secrets.provider_api_key"),
    ],
)
def test_invalid_secrets_file_entries_are_named(tmp_path: Path, content: str, key: str) -> None:
    secrets = write(tmp_path, "s.toml", content)
    with pytest.raises(ConfigError) as info:
        load_with(tmp_path, BASE_TOML, secrets)
    assert info.value.key == key


def test_missing_explicit_secrets_file_is_rejected(tmp_path: Path) -> None:
    missing = tmp_path / "gone.toml"
    config_file = write(tmp_path, "config.toml", BASE_TOML)
    with pytest.raises(ConfigError) as info:
        load_config(config_file, missing, env={})
    assert info.value.key == str(missing)


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o620])
def test_secrets_file_with_group_or_other_access_is_rejected(tmp_path: Path, mode: int) -> None:
    secrets = write(tmp_path, "s.toml", SECRETS_TOML, mode=mode)
    config_file = write(tmp_path, "config.toml", BASE_TOML)
    with pytest.raises(ConfigError) as info:
        load_config(config_file, secrets, env={})
    assert info.value.key == str(secrets)
    assert SECRET_VALUE not in str(info.value)


def test_secrets_file_mode_0600_is_accepted(tmp_path: Path) -> None:
    secrets = write(tmp_path, "s.toml", SECRETS_TOML, mode=0o600)
    config_file = write(tmp_path, "config.toml", BASE_TOML)
    cfg = load_config(config_file, secrets, env={})
    assert cfg.provider.api_key.reveal() == SECRET_VALUE


# --- redaction and immutability ---------------------------------------------------------------


def test_repr_and_str_hide_secret_values(tmp_path: Path) -> None:
    toml = edit(BASE_TOML, "port = 6199", 'port = 6199\ntoken_ref = "napcat_token"')
    secrets = write(
        tmp_path,
        "s.toml",
        f'[secrets]\nprovider_api_key = "{SECRET_VALUE}"\nnapcat_token = "tok-LEAK-CHECK"\n',
    )
    cfg = load_with(tmp_path, toml, secrets)

    for text in (repr(cfg), str(cfg), repr(cfg.provider), repr(cfg.napcat)):
        assert SECRET_VALUE not in text
        assert "tok-LEAK-CHECK" not in text
    assert repr(cfg.provider.api_key) == "Secret(***)"
    assert "Secret(***)" in repr(cfg.provider)
    assert SECRET_VALUE not in str(cfg.provider.api_key)


def test_secret_type_repr_and_reveal() -> None:
    secret = Secret("abc")
    assert repr(secret) == "Secret(***)"
    assert secret.reveal() == "abc"


def test_config_is_frozen(tmp_path: Path, secrets_file: Path) -> None:
    cfg = load_with(tmp_path, BASE_TOML, secrets_file)
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.napcat.port = 1  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.scope = cfg.scope  # type: ignore[misc]


def test_wire_api_defaults_to_responses(tmp_path: Path, secrets_file: Path) -> None:
    cfg = load_with(tmp_path, BASE_TOML, secrets_file)
    assert cfg.provider.wire_api == "responses"


@pytest.mark.parametrize("value", ["responses", "chat"])
def test_wire_api_accepts_known_values(tmp_path: Path, secrets_file: Path, value: str) -> None:
    toml = edit(BASE_TOML, "model = ", f'wire_api = "{value}"\nmodel = ')
    cfg = load_with(tmp_path, toml, secrets_file)
    assert cfg.provider.wire_api == value


def test_wire_api_unknown_value_is_named(tmp_path: Path, secrets_file: Path) -> None:
    toml = edit(BASE_TOML, "model = ", 'wire_api = "grpc"\nmodel = ')
    with pytest.raises(ConfigError) as info:
        load_with(tmp_path, toml, secrets_file)
    assert info.value.key == "provider.wire_api"
