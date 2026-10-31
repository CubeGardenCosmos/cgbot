"""Configuration: TOML file + secrets, loaded once at startup (no hot reload).

File format (see ``config.example.toml`` at the repo root for a commented copy):

- ``[napcat]``: ``host`` (default 127.0.0.1), ``port`` (required), ``path`` (default ``/ws``),
  ``token_ref`` (optional secret name; omitted means no token check).
- ``[scope]``: ``friends`` and ``groups`` (QQ ids, default empty = deny all), ``all_friends``
  (bool, default false: true also admits every real QQ friend in private chat; temporary
  sessions do not count), ``owner`` (optional identity marker, does not extend the allowlist).
- ``[provider]``: ``base_url`` (http/https), ``model``, ``context_window`` (positive int, required),
  ``api_key_ref`` (required secret name);
  ``[provider.reasoning]`` tiers ``summary`` (low), ``private`` (high), ``planning`` (max),
  ``group`` (low), passed through to the upstream.
- ``[storage]``: ``data_dir`` (default ``data``, relative to the config file's directory). It
  holds one SQLite file per domain: ``profile.sqlite`` (identity/channels), ``sessions.sqlite``
  (chat records), ``scheduler.sqlite``, and ``<module>.sqlite`` for later modules.
- ``[group]``: ``wake_keywords`` (default ``["大肥鱼"]``), ``mute_keywords`` (default empty).
- ``[logging]``: ``level`` (default ``INFO``).

Secrets: the config only names them. Values come from the secrets TOML ``[secrets]`` table
(path: ``secrets_path`` argument, else env ``CGBOT_SECRETS_FILE``; must be mode 0600) and can
be overridden per name by env ``CGBOT_SECRET_<NAME>`` (name upper-cased).

Any unknown key or invalid value raises :class:`ConfigError` naming the dotted key path.
"""

from cgbot.config.loader import SECRET_ENV_PREFIX, SECRETS_FILE_ENV, load_config
from cgbot.config.settings import (
    Config,
    ConfigError,
    GroupConfig,
    LoggingConfig,
    NapCatConfig,
    ProviderConfig,
    ReasoningTiers,
    Scope,
    ScopeConfig,
    Secret,
    StoragePaths,
)

__all__ = [
    "SECRETS_FILE_ENV",
    "SECRET_ENV_PREFIX",
    "Config",
    "ConfigError",
    "GroupConfig",
    "LoggingConfig",
    "NapCatConfig",
    "ProviderConfig",
    "ReasoningTiers",
    "Scope",
    "ScopeConfig",
    "Secret",
    "StoragePaths",
    "load_config",
]
