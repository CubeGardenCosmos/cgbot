"""TOML + secrets loading with strict validation (unknown keys and bad values are errors)."""

from __future__ import annotations

import os
import re
import stat
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Final

from cgbot.config.settings import (
    SEARCH_ENGINE_NAMES,
    WIRE_APIS,
    Config,
    ConfigError,
    GroupConfig,
    LoggingConfig,
    NapCatConfig,
    ProviderConfig,
    ReasoningTiers,
    ScopeConfig,
    SearchConfig,
    SearchGeminiConfig,
    Secret,
    StoragePaths,
    WireApi,
)
from cgbot.voice.config import load_voice_config

if TYPE_CHECKING:
    # Type-only: coding/config.py imports cgbot.config.settings (see _coding for the lazy import).
    from cgbot.coding.config import CodingConfig, ImageGenConfig

SECRETS_FILE_ENV: Final = "CGBOT_SECRETS_FILE"
SECRET_ENV_PREFIX: Final = "CGBOT_SECRET_"

_MISSING: Final = object()
_SECRET_NAME: Final = re.compile(r"[a-z][a-z0-9_]*")
_QQ_ID: Final = re.compile(r"[1-9][0-9]*")
_LOG_LEVELS: Final = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
_SECRET_REF_PREFIX: Final = "secret:"
_CONTAINER_PREFIX: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


def load_config(
    path: str | os.PathLike[str],
    secrets_path: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] = os.environ,
) -> Config:
    """Load and validate the config file, then resolve every referenced secret.

    Secrets come from the secrets TOML (``[secrets]`` table) and can be overridden per name by
    ``CGBOT_SECRET_<NAME>`` in ``env``. The secrets file is ``secrets_path``, else
    ``$CGBOT_SECRETS_FILE``; it must be mode 0600. Relative ``storage.data_dir`` is resolved
    against the config file's directory; each domain gets its own SQLite file under it
    (``profile.sqlite``, ``sessions.sqlite``, ``scheduler.sqlite``, ``<module>.sqlite``), and
    files are never shared. Loading happens once; there is no hot reload.

    Raises:
        ConfigError: missing/invalid/unknown key, unreadable file, or unresolved secret.
    """
    config_path = Path(path)
    root = _Table(_read_toml(config_path), "")

    if secrets_path is not None:
        secret_file: str | os.PathLike[str] | None = secrets_path
    else:
        secret_file = env.get(SECRETS_FILE_ENV) or None
    file_values = {} if secret_file is None else _read_secrets(Path(secret_file))
    source = _SecretSource(file_values, env)

    napcat = _napcat(root.sub("napcat", required=True), source)
    scope = _scope(root.sub("scope"))
    provider = _provider(root.sub("provider", required=True), source)
    group = _group(root.sub("group"))
    storage = _storage(root.sub("storage"), config_path.parent)
    logging_cfg = _logging(root.sub("logging"))
    search = _search(root.sub("search"), source)
    voice_raw = root.raw_table("voice")
    coding_raw = root.raw_table("coding")
    root.close()

    return Config(
        napcat=napcat,
        scope=scope,
        provider=provider,
        group=group,
        storage=storage,
        logging=logging_cfg,
        search=search,
        voice=load_voice_config(voice_raw) if voice_raw is not None else None,
        coding=(
            _coding(coding_raw, source, config_path.parent, provider.context_window)
            if coding_raw is not None
            else None
        ),
    )


def _napcat(t: _Table, source: _SecretSource) -> NapCatConfig:
    host = t.text_or("host", "127.0.0.1")
    port = t.port("port")
    path = t.text_or("path", "/ws")
    if not path.startswith("/"):
        raise t.error("path", "须以 / 开头")
    token_ref = t.optional_text("token_ref")
    token = None if token_ref is None else source.resolve(token_ref, t.key("token_ref"))
    t.close()
    return NapCatConfig(host=host, port=port, path=path, token=token)


def _scope(t: _Table) -> ScopeConfig:
    owner = t.optional_qq("owner")
    friends = t.qq_ids("friends")
    groups = t.qq_ids("groups")
    all_friends = t.bool_or("all_friends", False)
    t.close()
    return ScopeConfig(owner=owner, friends=friends, groups=groups, all_friends=all_friends)


def _provider(t: _Table, source: _SecretSource) -> ProviderConfig:
    base_url = t.text("base_url")
    if not base_url.startswith(("http://", "https://")):
        raise t.error("base_url", "须以 http:// 或 https:// 开头")
    model = t.text("model")
    context_window = t.positive_int("context_window")
    api_key = source.resolve(t.text("api_key_ref"), t.key("api_key_ref"))
    wire_text = t.text_or("wire_api", "responses")
    if wire_text not in WIRE_APIS:
        raise t.error("wire_api", "须为 responses 或 chat")
    wire_api: WireApi = "chat" if wire_text == "chat" else "responses"

    defaults = ReasoningTiers()
    tiers = t.sub("reasoning")
    reasoning = ReasoningTiers(
        summary=tiers.text_or("summary", defaults.summary),
        private=tiers.text_or("private", defaults.private),
        planning=tiers.text_or("planning", defaults.planning),
        group=tiers.text_or("group", defaults.group),
    )
    tiers.close()
    t.close()
    return ProviderConfig(
        base_url=base_url,
        model=model,
        context_window=context_window,
        api_key=api_key,
        reasoning=reasoning,
        wire_api=wire_api,
    )


def _group(t: _Table) -> GroupConfig:
    wake = t.text_list("wake_keywords", ("大肥鱼",))
    mute = t.text_list("mute_keywords", ())
    defaults = GroupConfig(wake_keywords=wake, mute_keywords=mute)
    group = GroupConfig(
        wake_keywords=wake,
        mute_keywords=mute,
        idle_timeout_seconds=t.positive_number_or(
            "idle_timeout_seconds", defaults.idle_timeout_seconds
        ),
        max_turns=t.positive_int_or("max_turns", defaults.max_turns),
        skill_topics=t.positive_int_or("skill_topics", defaults.skill_topics),
        skill_memes=t.positive_int_or("skill_memes", defaults.skill_memes),
        skill_rules=t.positive_int_or("skill_rules", defaults.skill_rules),
        skill_members=t.positive_int_or("skill_members", defaults.skill_members),
        skill_habits_per_member=t.positive_int_or(
            "skill_habits_per_member", defaults.skill_habits_per_member
        ),
    )
    t.close()
    return group


def _storage(t: _Table, base_dir: Path) -> StoragePaths:
    data_dir = t.text_or("data_dir", "data")
    t.close()
    return StoragePaths(data_dir=base_dir / data_dir)


def _logging(t: _Table) -> LoggingConfig:
    level = t.text_or("level", "INFO").upper()
    if level not in _LOG_LEVELS:
        raise t.error("level", "须为 DEBUG/INFO/WARNING/ERROR/CRITICAL 之一")
    t.close()
    return LoggingConfig(level=level)


def _search(t: _Table, source: _SecretSource) -> SearchConfig:
    engines = t.text_list("engines", SEARCH_ENGINE_NAMES)
    if not engines or any(name not in SEARCH_ENGINE_NAMES for name in engines):
        raise t.error("engines", "须为 bing/baidu/bilibili 中的若干个（至少一个）")
    default_engine = t.text_or("default_engine", "")
    if default_engine and default_engine not in engines:
        raise t.error("default_engine", "须为已启用的引擎之一")
    timeout_seconds = t.positive_number_or("timeout_seconds", 30.0)
    default_count = t.positive_int_or("default_count", 5)
    max_count = t.positive_int_or("max_count", 10)
    if default_count > max_count:
        raise t.error("default_count", "不能大于 max_count")
    baidu_ref = t.optional_text("baidu_cookies_ref")
    baidu_cookies = (
        None if baidu_ref is None else source.resolve(baidu_ref, t.key("baidu_cookies_ref"))
    )
    bilibili_ref = t.optional_text("bilibili_cookies_ref")
    bilibili_cookies = (
        None
        if bilibili_ref is None
        else source.resolve(bilibili_ref, t.key("bilibili_cookies_ref"))
    )
    gemini = _search_gemini(t.sub("gemini"), source)
    t.close()
    return SearchConfig(
        engines=engines,
        default_engine=default_engine,
        timeout_seconds=timeout_seconds,
        default_count=default_count,
        max_count=max_count,
        baidu_cookies=baidu_cookies,
        bilibili_cookies=bilibili_cookies,
        gemini=gemini,
    )


def _search_gemini(t: _Table, source: _SecretSource) -> SearchGeminiConfig:
    enabled = t.bool_or("enabled", False)
    endpoint = t.text_or("endpoint", "")
    model = t.text_or("model", "gemini-2.5-flash")
    timeout_seconds = t.positive_number_or("timeout_seconds", 90.0)
    token_ref = t.optional_text("token_ref")
    token = None if token_ref is None else source.resolve(token_ref, t.key("token_ref"))
    if enabled:
        if not endpoint.startswith(("http://", "https://")):
            raise t.error("endpoint", "启用时须以 http:// 或 https:// 开头")
        if token is None:
            raise t.error("token_ref", "启用时必须配置")
    t.close()
    return SearchGeminiConfig(
        enabled=enabled,
        endpoint=endpoint,
        token=token,
        model=model,
        timeout_seconds=timeout_seconds,
    )


def _coding(
    raw: Mapping[str, object], source: _SecretSource, base_dir: Path, context_window: int
) -> CodingConfig | None:
    """``[coding]``: fully validated even when ``enabled = false``; disabled returns None."""
    # Lazy import: coding/config.py imports cgbot.config.settings, and importing cgbot.config runs
    # this module, so a module-level import would be cyclic when cgbot.coding is imported first.
    from cgbot.coding import config as coding_cfg

    t = _Table(raw, "coding")
    enabled = t.bool_or("enabled", True)
    token_secret = t.secret_ref("token_secret", source)

    http_t = t.sub("http", required=True)
    http = coding_cfg.HttpConfig(
        host=http_t.text("host"),
        port=http_t.port("port"),
        public_base_url=http_t.http_url("public_base_url"),
    )
    http_t.close()

    docker_t = t.sub("docker", required=True)
    prefix = docker_t.text_or("container_prefix", "cgbot-sbx-")
    if _CONTAINER_PREFIX.fullmatch(prefix) is None:
        raise docker_t.error("container_prefix", "须以字母或数字开头,仅含字母、数字、_ . -")
    docker = coding_cfg.DockerConfig(
        host=docker_t.http_url("host"),
        sandbox_image=docker_t.text("sandbox_image"),
        network=docker_t.text("network"),
        host_data_dir=docker_t.absolute_path("host_data_dir"),
        egress_proxy=docker_t.proxy_url_or("egress_proxy", ""),
        idle_seconds=docker_t.positive_number("idle_seconds"),
        container_prefix=prefix,
    )
    docker_t.close()

    codex_t = t.sub("codex", required=True)
    codex = coding_cfg.CodexConfig(
        model=codex_t.text("model"),
        reasoning_effort=codex_t.text("reasoning_effort"),
        appserver_port=codex_t.port("appserver_port"),
        context_window=codex_t.positive_int_or("context_window", context_window),
    )
    codex_t.close()

    conc_t = t.sub("concurrency", required=True)
    concurrency = coding_cfg.ConcurrencyConfig(
        per_user=conc_t.positive_int("per_user"),
        degraded=conc_t.positive_int("degraded"),
        degrade_failures=conc_t.positive_int("degrade_failures"),
        degrade_window_seconds=conc_t.positive_number("degrade_window_seconds"),
        recover_after_seconds=conc_t.positive_number("recover_after_seconds"),
    )
    conc_t.close()

    bridge_t = t.sub("bridge", required=True)
    bridge = coding_cfg.BridgeConfig(
        max_file_bytes=bridge_t.positive_int("max_file_bytes"),
        chunk_bytes=bridge_t.positive_int("chunk_bytes"),
        temp_dir=bridge_t.path_under("temp_dir", base_dir),
        file_ttl_seconds=bridge_t.positive_number("file_ttl_seconds"),
    )
    bridge_t.close()

    imagegen = _imagegen(t, source)
    t.close()
    if not enabled:
        return None
    return coding_cfg.CodingConfig(
        token_secret=token_secret,
        http=http,
        docker=docker,
        codex=codex,
        concurrency=concurrency,
        bridge=bridge,
        imagegen=imagegen,
    )


def _imagegen(t: _Table, source: _SecretSource) -> ImageGenConfig | None:
    from cgbot.coding import config as coding_cfg  # lazy: same cycle as in _coding

    ig = t.optional_sub("imagegen")
    if ig is None:
        return None
    cfg = coding_cfg.ImageGenConfig(
        base_url=ig.http_url("base_url"),
        api_key=ig.secret_ref("api_key", source),
        model=ig.text("model"),
        mode=ig.text("mode"),
        timeout_seconds=ig.positive_number("timeout_seconds"),
        proxy=ig.proxy_url_or("proxy", ""),
        max_concurrency=ig.positive_int("max_concurrency"),
    )
    ig.close()
    return cfg


class _SecretSource:
    """Resolves secret names; ``CGBOT_SECRET_<NAME>`` wins over the secrets file."""

    __slots__ = ("_env", "_file")

    def __init__(self, file_values: Mapping[str, str], env: Mapping[str, str]) -> None:
        self._file = file_values
        self._env = env

    def resolve(self, ref: str, key: str) -> Secret:
        if _SECRET_NAME.fullmatch(ref) is None:
            raise ConfigError(key, f"密钥名 {ref!r} 须为小写字母开头的字母、数字或下划线")
        env_name = SECRET_ENV_PREFIX + ref.upper()
        if env_name in self._env:
            raw = self._env[env_name]
        elif ref in self._file:
            raw = self._file[ref]
        else:
            raise ConfigError(
                key,
                f"未找到密钥 {ref!r}:请在 secrets 文件的 [secrets] 中定义,"
                f"或设置环境变量 {env_name}",
            )
        value = raw.strip()
        if not value:
            raise ConfigError(key, f"密钥 {ref!r} 为空")
        return Secret(value)


class _Table:
    """One TOML table. Records consumed keys so :meth:`close` can reject unknown ones."""

    __slots__ = ("_data", "_path", "_seen")

    def __init__(self, data: Mapping[str, object], path: str) -> None:
        self._data = data
        self._path = path
        self._seen: set[str] = set()

    def key(self, name: str) -> str:
        return f"{self._path}.{name}" if self._path else name

    def error(self, name: str, message: str) -> ConfigError:
        return ConfigError(self.key(name), message)

    def _take(self, name: str) -> object:
        self._seen.add(name)
        return self._data.get(name, _MISSING)

    def _required(self, name: str) -> object:
        value = self._take(name)
        if value is _MISSING:
            raise self.error(name, "缺少必需项")
        return value

    def _text(self, name: str, value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            raise self.error(name, "须为非空字符串")
        return value.strip()

    def sub(self, name: str, *, required: bool = False) -> _Table:
        value = self._take(name)
        if value is _MISSING:
            if required:
                raise self.error(name, "缺少必需的配置节")
            value = {}
        if not isinstance(value, dict):
            raise self.error(name, "须为配置节(表)")
        return _Table(value, self.key(name))

    def entries(self) -> list[tuple[str, object]]:
        """All keys of this table (marks them consumed). Used for the secrets table."""
        self._seen.update(self._data)
        return list(self._data.items())

    def text(self, name: str) -> str:
        return self._text(name, self._required(name))

    def text_or(self, name: str, default: str) -> str:
        value = self._take(name)
        return default if value is _MISSING else self._text(name, value)

    def optional_text(self, name: str) -> str | None:
        value = self._take(name)
        return None if value is _MISSING else self._text(name, value)

    def port(self, name: str) -> int:
        value = self._required(name)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
            raise self.error(name, "须为 1–65535 的整数")
        return value

    def positive_int(self, name: str) -> int:
        value = self._required(name)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise self.error(name, "须为正整数")
        return value

    def positive_int_or(self, name: str, default: int) -> int:
        value = self._take(name)
        if value is _MISSING:
            return default
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise self.error(name, "须为正整数")
        return value

    def positive_number_or(self, name: str, default: float) -> float:
        value = self._take(name)
        if value is _MISSING:
            return default
        if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
            raise self.error(name, "须为正数")
        return float(value)

    def raw_table(self, name: str) -> dict[str, object] | None:
        """Nested table kept raw for a module that validates its own keys (``voice``)."""
        value = self._take(name)
        if value is _MISSING:
            return None
        if not isinstance(value, dict):
            raise self.error(name, "须为配置节(表)")
        return value

    def optional_qq(self, name: str) -> str | None:
        value = self._take(name)
        return None if value is _MISSING else _qq_id(self.key(name), value)

    def qq_ids(self, name: str) -> frozenset[str]:
        value = self._take(name)
        if value is _MISSING:
            return frozenset()
        if not isinstance(value, list):
            raise self.error(name, "须为 QQ 号数组")
        return frozenset(_qq_id(f"{self.key(name)}[{i}]", item) for i, item in enumerate(value))

    def text_list(self, name: str, default: tuple[str, ...]) -> tuple[str, ...]:
        value = self._take(name)
        if value is _MISSING:
            return default
        if not isinstance(value, list):
            raise self.error(name, "须为字符串数组")
        items = (self._text(f"{name}[{i}]", item) for i, item in enumerate(value))
        return tuple(dict.fromkeys(items))

    def bool_or(self, name: str, default: bool) -> bool:
        value = self._take(name)
        if value is _MISSING:
            return default
        if not isinstance(value, bool):
            raise self.error(name, "须为 true 或 false")
        return value

    def positive_number(self, name: str) -> float:
        value = self._required(name)
        if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
            raise self.error(name, "须为正数")
        return float(value)

    def http_url(self, name: str) -> str:
        value = self.text(name)
        if not value.startswith(("http://", "https://")):
            raise self.error(name, "须以 http:// 或 https:// 开头")
        return value.rstrip("/")

    def proxy_url_or(self, name: str, default: str) -> str:
        """Optional proxy URL; the empty string means no proxy."""
        value = self._take(name)
        if value is _MISSING:
            return default
        if not isinstance(value, str):
            raise self.error(name, "须为字符串")
        text = value.strip()
        if text and not text.startswith(("http://", "https://")):
            raise self.error(name, "须为空或以 http:// 或 https:// 开头")
        return text

    def absolute_path(self, name: str) -> Path:
        path = Path(self.text(name))
        if not path.is_absolute():
            raise self.error(name, "须为绝对路径")
        return path

    def path_under(self, name: str, base: Path) -> Path:
        """Relative paths resolve against ``base`` (the config file's directory)."""
        path = Path(self.text(name))
        return path if path.is_absolute() else base / path

    def secret_ref(self, name: str, source: _SecretSource) -> Secret:
        value = self.text(name)
        if not value.startswith(_SECRET_REF_PREFIX):
            raise self.error(name, "须为 secret:<名称> 形式")
        return source.resolve(value[len(_SECRET_REF_PREFIX) :], self.key(name))

    def optional_sub(self, name: str) -> _Table | None:
        value = self._take(name)
        if value is _MISSING:
            return None
        if not isinstance(value, dict):
            raise self.error(name, "须为配置节(表)")
        return _Table(value, self.key(name))

    def close(self) -> None:
        extra = sorted(set(self._data) - self._seen)
        if extra:
            raise self.error(extra[0], "未知配置键")


def _qq_id(key: str, value: object) -> str:
    if isinstance(value, bool):
        text = ""
    elif isinstance(value, int):
        text = str(value)
    elif isinstance(value, str):
        text = value.strip()
    else:
        text = ""
    if _QQ_ID.fullmatch(text) is None:
        raise ConfigError(key, "须为纯数字 QQ 号")
    return text


def _read_toml(path: Path) -> dict[str, object]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(str(path), "文件不存在") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(str(path), f"TOML 语法错误: {exc}") from exc
    except OSError as exc:
        raise ConfigError(str(path), f"无法读取: {exc.strerror}") from exc


def _read_secrets(path: Path) -> dict[str, str]:
    try:
        mode = path.stat().st_mode
    except OSError as exc:
        raise ConfigError(str(path), f"无法访问 secrets 文件: {exc.strerror}") from exc
    if stat.S_IMODE(mode) & 0o077:
        raise ConfigError(str(path), "secrets 文件权限过宽,须为 0600(组和其他用户不得有任何权限)")

    root = _Table(_read_toml(path), "")
    entries = root.sub("secrets").entries()
    root.close()

    values: dict[str, str] = {}
    for name, value in entries:
        key = f"secrets.{name}"
        if _SECRET_NAME.fullmatch(name) is None:
            raise ConfigError(key, "密钥名须为小写字母开头的字母、数字或下划线")
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(key, "密钥值须为非空字符串")
        values[name] = value.strip()
    return values
