"""R5.1: [coding] config loading, validation, and secret references (offline)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from cgbot.config import Config, ConfigError, load_config

PROVIDER_SECRET = "provider-value-XYZ"
TOKEN_VALUE = "coding-token-value-ABC"
IMAGEGEN_VALUE = "imagegen-value-DEF"

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
enabled = true
token_secret = "secret:coding_token"

[coding.http]
host = "0.0.0.0"
port = 8000
public_base_url = "http://cgbot:8000/"

[coding.docker]
host = "http://caged-docker-proxy:2375"
sandbox_image = "cgbot-sandbox:latest"
network = "caged_internal"
host_data_dir = "/data/cgbot-data"
egress_proxy = "http://caged-egress:7890"
idle_seconds = 3600
container_prefix = "cgbot-sbx-"

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

[coding.imagegen]
base_url = "https://img.example/v1"
api_key = "secret:imagegen_key"
model = "img-model"
mode = "responses"
timeout_seconds = 600
proxy = ""
max_concurrency = 2
"""

SECRETS_TOML = f"""
[secrets]
provider_api_key = "{PROVIDER_SECRET}"
coding_token = "{TOKEN_VALUE}"
imagegen_key = "{IMAGEGEN_VALUE}"
"""


def write_files(
    tmp_path: Path, coding: str | None, secrets: str = SECRETS_TOML
) -> tuple[Path, Path]:
    config = tmp_path / "config.toml"
    config.write_text(BASE_TOML + (coding or ""), encoding="utf-8")
    secrets_path = tmp_path / "secrets.toml"
    secrets_path.write_text(secrets, encoding="utf-8")
    secrets_path.chmod(0o600)
    return config, secrets_path


def mutate(old: str, new: str) -> str:
    assert old in CODING_TOML, f"test template has no {old!r}"
    return CODING_TOML.replace(old, new, 1)


def load(tmp_path: Path, coding: str | None, env: dict[str, str] | None = None) -> Config:
    config, secrets = write_files(tmp_path, coding)
    return load_config(config, secrets, env={} if env is None else env)


def test_full_coding_section_loads_with_secrets_resolved(tmp_path: Path) -> None:
    coding = load(tmp_path, CODING_TOML).coding
    assert coding is not None
    assert coding.token_secret.reveal() == TOKEN_VALUE
    assert coding.http.host == "0.0.0.0"
    assert coding.http.port == 8000
    assert coding.http.public_base_url == "http://cgbot:8000"
    assert coding.docker.host == "http://caged-docker-proxy:2375"
    assert coding.docker.host_data_dir == Path("/data/cgbot-data")
    assert coding.docker.egress_proxy == "http://caged-egress:7890"
    assert coding.docker.container_prefix == "cgbot-sbx-"
    assert coding.codex.appserver_port == 8765
    assert coding.concurrency.per_user == 4
    assert coding.bridge.max_file_bytes == 536870912
    assert coding.imagegen is not None
    assert coding.imagegen.api_key.reveal() == IMAGEGEN_VALUE
    assert coding.imagegen.base_url == "https://img.example/v1"
    assert coding.imagegen.max_concurrency == 2


def test_secret_values_never_appear_in_repr(tmp_path: Path) -> None:
    rendered = repr(load(tmp_path, CODING_TOML).coding)
    assert TOKEN_VALUE not in rendered
    assert IMAGEGEN_VALUE not in rendered


def test_relative_temp_dir_resolves_against_config_directory(tmp_path: Path) -> None:
    coding = load(tmp_path, CODING_TOML).coding
    assert coding is not None
    assert coding.bridge.temp_dir == tmp_path / "data/bridge"


def test_absent_coding_section_means_none(tmp_path: Path) -> None:
    assert load(tmp_path, None).coding is None


def test_absent_imagegen_means_none(tmp_path: Path) -> None:
    text = CODING_TOML.split("[coding.imagegen]")[0]
    coding = load(tmp_path, text).coding
    assert coding is not None
    assert coding.imagegen is None


def test_disabled_section_returns_none_but_is_still_validated(tmp_path: Path) -> None:
    disabled = mutate("enabled = true", "enabled = false")
    assert load(tmp_path, disabled).coding is None

    broken = disabled.replace("port = 8000", 'port = "eighty"')
    with pytest.raises(ConfigError, match=r"^coding\.http\.port:"):
        load(tmp_path, broken)


def test_env_secret_overrides_secrets_file(tmp_path: Path) -> None:
    coding = load(tmp_path, CODING_TOML, env={"CGBOT_SECRET_CODING_TOKEN": "from-env"}).coding
    assert coding is not None
    assert coding.token_secret.reveal() == "from-env"


def test_missing_secret_is_rejected(tmp_path: Path) -> None:
    config, secrets = write_files(
        tmp_path, CODING_TOML, secrets=SECRETS_TOML.replace("coding_token", "other")
    )
    with pytest.raises(ConfigError, match=r"^coding\.token_secret:"):
        load_config(config, secrets, env={})


@pytest.mark.parametrize(
    ("coding", "key"),
    [
        (
            mutate('token_secret = "secret:coding_token"', 'token_secret = "coding_token"'),
            "coding.token_secret",
        ),
        (mutate("[coding.codex]", "[coding.codex]\nbogus = 1"), "coding.codex.bogus"),
        (mutate("enabled = true", "enabled = true\nbogus = 1"), "coding.bogus"),
        (mutate("[coding.http]", "[coding.httpx]"), "coding.http"),
        (mutate("[coding.codex]", "[coding.codex_x]"), "coding.codex"),
        (mutate('host = "0.0.0.0"\n', ""), "coding.http.host"),
        (
            mutate('public_base_url = "http://cgbot:8000/"', 'public_base_url = "ftp://x"'),
            "coding.http.public_base_url",
        ),
        (
            mutate('host = "http://caged-docker-proxy:2375"', 'host = "caged:2375"'),
            "coding.docker.host",
        ),
        (
            mutate('host_data_dir = "/data/cgbot-data"', 'host_data_dir = "relative/dir"'),
            "coding.docker.host_data_dir",
        ),
        (
            mutate('egress_proxy = "http://caged-egress:7890"', 'egress_proxy = "socks5://x"'),
            "coding.docker.egress_proxy",
        ),
        (
            mutate('container_prefix = "cgbot-sbx-"', 'container_prefix = "-bad"'),
            "coding.docker.container_prefix",
        ),
        (mutate("idle_seconds = 3600", "idle_seconds = 0"), "coding.docker.idle_seconds"),
        (mutate("per_user = 4", "per_user = 0"), "coding.concurrency.per_user"),
        (
            mutate("file_ttl_seconds = 86400", "file_ttl_seconds = -1"),
            "coding.bridge.file_ttl_seconds",
        ),
        (
            mutate('api_key = "secret:imagegen_key"', 'api_key = "imagegen_key"'),
            "coding.imagegen.api_key",
        ),
        (mutate("max_concurrency = 2", "max_concurrency = 0"), "coding.imagegen.max_concurrency"),
    ],
)
def test_invalid_coding_section_is_rejected_with_key_path(
    tmp_path: Path, coding: str, key: str
) -> None:
    with pytest.raises(ConfigError, match=rf"^{re.escape(key)}:"):
        load(tmp_path, coding)


def test_missing_required_codex_table_is_rejected(tmp_path: Path) -> None:
    text = re.sub(r"\[coding\.codex\].*?(?=\[coding\.concurrency\])", "", CODING_TOML, flags=re.S)
    with pytest.raises(ConfigError, match=r"^coding\.codex:"):
        load(tmp_path, text)
