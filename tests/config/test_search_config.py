"""[search] config: defaults, validation, cookie/token secret resolution (offline)."""

from __future__ import annotations

from pathlib import Path

import pytest

from cgbot.config import Config, ConfigError, load_config
from cgbot.config.settings import SEARCH_ENGINE_NAMES

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
baidu_cookies = "BAIDUID=baidu-secret"
bilibili_cookies = "buvid3=bili-secret"
"""


def _load(tmp_path: Path, search: str = "", secrets: str = SECRETS) -> Config:
    config_file = tmp_path / "config.toml"
    secrets_file = tmp_path / "secrets.toml"
    data_dir = tmp_path / "data"
    config_file.write_text(
        BASE_TOML + f'\n[storage]\ndata_dir = "{data_dir}"\n' + search, encoding="utf-8"
    )
    secrets_file.write_text(secrets, encoding="utf-8")
    secrets_file.chmod(0o600)
    return load_config(config_file, secrets_file, env={})


def test_missing_search_section_uses_defaults(tmp_path: Path) -> None:
    search = _load(tmp_path).search
    assert search.engines == SEARCH_ENGINE_NAMES
    assert search.default_engine == ""
    assert search.default_count == 5
    assert search.max_count == 10
    assert search.baidu_cookies is None
    assert search.bilibili_cookies is None
    assert search.gemini.enabled is False
    assert search.gemini.model == "gemini-3.1-flash-lite"
    assert search.gemini.timeout_seconds == 90.0


def test_engines_subset_and_default_engine(tmp_path: Path) -> None:
    search = _load(
        tmp_path,
        '\n[search]\nengines = ["baidu", "bing"]\ndefault_engine = "bing"\n',
    ).search
    assert search.engines == ("baidu", "bing")
    assert search.default_engine == "bing"


def test_cookie_refs_resolve_through_secrets(tmp_path: Path) -> None:
    search = _load(
        tmp_path,
        '\n[search]\nbaidu_cookies_ref = "baidu_cookies"\n'
        'bilibili_cookies_ref = "bilibili_cookies"\n',
    ).search
    assert search.baidu_cookies is not None
    assert search.baidu_cookies.reveal() == "BAIDUID=baidu-secret"
    assert search.bilibili_cookies is not None
    assert search.bilibili_cookies.reveal() == "buvid3=bili-secret"


def test_gemini_enabled_reads_base_url_model_and_timeout(tmp_path: Path) -> None:
    search = _load(
        tmp_path,
        '\n[search.gemini]\nenabled = true\nbase_url = "https://relay.example/v1beta"\n'
        'model = "gemini-other"\ntimeout_seconds = 45\n',
    ).search
    assert search.gemini.enabled is True
    assert search.gemini.base_url == "https://relay.example/v1beta"
    assert search.gemini.model == "gemini-other"
    assert search.gemini.timeout_seconds == 45.0


def test_gemini_disabled_needs_no_base_url(tmp_path: Path) -> None:
    gemini = _load(tmp_path, "\n[search.gemini]\nenabled = false\n").search.gemini
    assert gemini.enabled is False
    assert gemini.base_url == ""


@pytest.mark.parametrize(
    "search",
    [
        '\n[search]\nengines = ["google"]\n',
        "\n[search]\nengines = []\n",
        '\n[search]\nengines = ["bing"]\ndefault_engine = "baidu"\n',
        "\n[search]\ndefault_count = 20\nmax_count = 10\n",
        '\n[search.gemini]\nenabled = true\nbase_url = "relay.example/v1beta"\n',
        "\n[search.gemini]\nenabled = true\n",
        '\n[search.gemini]\nenabled = true\nbase_url = "https://relay.example/v1beta"\n'
        "timeout_seconds = 0\n",
        '\n[search.gemini]\nenabled = true\nendpoint = "http://relay.example:8790/search"\n'
        'base_url = "https://relay.example/v1beta"\n',
        '\n[search.gemini]\nenabled = true\nbase_url = "https://relay.example/v1beta"\n'
        'token_ref = "gemini_search_token"\n',
        '\n[search]\nbaidu_cookies_ref = "no_such_secret"\n',
    ],
)
def test_invalid_search_config_is_rejected(tmp_path: Path, search: str) -> None:
    with pytest.raises(ConfigError):
        _load(tmp_path, search)
