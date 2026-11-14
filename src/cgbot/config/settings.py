"""Immutable configuration objects produced by :func:`cgbot.config.load_config`."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

if TYPE_CHECKING:
    # voice/config.py imports ConfigError from this module; a runtime import would be a cycle.
    from cgbot.character import CharacterCard
    from cgbot.coding.config import CodingConfig
    from cgbot.voice.config import VoiceConfig

Scope = Literal["private", "group"]
WireApi = Literal["responses", "chat", "gemini"]
WIRE_APIS: Final = ("responses", "chat", "gemini")
DEFAULT_TIMEZONE: Final = "UTC"


class ConfigError(Exception):
    """Configuration is missing, malformed, or contains unknown keys.

    ``key`` is the dotted key path (e.g. ``napcat.port``) or the file path for file-level
    failures. The message never contains secret values.
    """

    def __init__(self, key: str, message: str) -> None:
        super().__init__(f"{key}: {message}")
        self.key = key
        self.message = message


@dataclass(frozen=True, slots=True, repr=False)
class Secret:
    """A credential value. ``repr``/``str`` never reveal it; use :meth:`reveal` explicitly."""

    _value: str

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret(***)"


@dataclass(frozen=True, slots=True)
class NapCatConfig:
    """Reverse-WS server endpoint that NapCat connects to."""

    host: str
    port: int
    path: str
    token: Secret | None  # None: no bearer token is checked


@dataclass(frozen=True, slots=True)
class ScopeConfig:
    """Service allowlist. Anything not allowed is dropped before business logic and LLM."""

    owner: str | None
    friends: frozenset[str]
    groups: frozenset[str]
    all_friends: bool = False  # True: every real QQ friend (NapCat sub_type "friend") is in scope

    def allows(
        self, scope: Scope, user_id: str, group_id: str | None, *, is_friend: bool = False
    ) -> bool:
        """Private: sender must be in ``friends``, or a real friend when ``all_friends`` is on.

        ``is_friend`` is the platform fact that the private sender is a real friend (NapCat
        ``sub_type == "friend"``); temporary sessions opened from group members are not friends.
        Group: group must be configured. Group membership is not friend-gated: any member of a
        configured group is in scope. ``owner`` is an identity marker only and does not extend
        the allowlist.
        """
        if scope == "private":
            return user_id in self.friends or (self.all_friends and is_friend)
        if scope == "group":
            return group_id is not None and group_id in self.groups
        raise ValueError(f"unknown scope: {scope!r}")


@dataclass(frozen=True, slots=True)
class ReasoningTiers:
    """Per-purpose reasoning effort, passed through to the upstream as configured."""

    summary: str = "low"  # compaction summary
    private: str = "high"  # daily private chat
    planning: str = "max"  # task planning (not designed yet)
    group: str = "low"  # group chat turns


@dataclass(frozen=True, slots=True)
class VisionConfig:
    """``[provider.vision]``: send inbound chat images to the (multimodal) model.

    Off by default. Images are downscaled to ``max_edge`` and stored per scope; only the newest
    ``recent_image_turns`` image-bearing user turns are sent as images, older ones stay as the
    text marker. Stored images are kept at most ``retention_images`` per scope and
    ``retention_days`` days.
    """

    enabled: bool = False
    max_images_per_turn: int = 4
    recent_image_turns: int = 3
    max_edge: int = 1536
    retention_images: int = 200
    retention_days: int = 30


DEFAULT_RETRY_ATTEMPTS = 2
DEFAULT_RETRY_BACKOFF: tuple[float, ...] = (2.0, 5.0)


@dataclass(frozen=True, slots=True)
class FallbackModelConfig:
    """One ``[[provider.fallbacks]]`` entry, fully resolved (omitted keys already inherited from
    ``[provider]``). Retry settings and reasoning tiers are always the primary's."""

    model: str
    wire_api: WireApi
    base_url: str
    api_key: Secret
    context_window: int


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """Model provider; ``wire_api`` selects Responses, Chat Completions or native Gemini."""

    base_url: str
    model: str
    context_window: int
    api_key: Secret
    reasoning: ReasoningTiers
    wire_api: WireApi = "responses"  # wire format: Responses (default) or Chat Completions
    retry_attempts: int = DEFAULT_RETRY_ATTEMPTS  # extra tries after a transient failure
    retry_backoff: tuple[float, ...] = DEFAULT_RETRY_BACKOFF  # seconds before retry n; last repeats
    vision: VisionConfig = field(default_factory=lambda: VisionConfig())
    # Tried in order after ``model`` fails (every turn restarts at ``model``).
    fallbacks: tuple[FallbackModelConfig, ...] = ()


@dataclass(frozen=True, slots=True)
class GroupConfig:
    """Group chat settings: keyword rules, session lifetime, group skill-card limits, and the
    IANA ``timezone`` that defines the local clock for gate hours, log timestamps and the message
    timestamps the model sees."""

    wake_keywords: tuple[str, ...]
    mute_keywords: tuple[str, ...]
    idle_timeout_seconds: float = 600.0  # [推论] group session ends after this much quiet
    max_turns: int = 30  # [推论] group session ends after this many turns
    skill_topics: int = 8  # [推论] skill-card limits (SkillLimits)
    skill_memes: int = 10
    skill_rules: int = 10
    skill_members: int = 20
    skill_habits_per_member: int = 5
    timezone: str = DEFAULT_TIMEZONE  # IANA name, validated by the loader


@dataclass(frozen=True, slots=True)
class StoragePaths:
    """Per-domain SQLite files under ``data_dir`` (round 20: no shared database).

    User profile/config data (identity, channels) go to ``profile.sqlite``; chat records go
    to ``sessions.sqlite``; every other module owns its own file (``scheduler.sqlite`` today,
    memory later). Modules never open a file belonging to another domain.
    """

    data_dir: Path

    @property
    def profile(self) -> Path:
        return self.data_dir / "profile.sqlite"

    @property
    def sessions(self) -> Path:
        return self.data_dir / "sessions.sqlite"

    @property
    def scheduler(self) -> Path:
        return self.data_dir / "scheduler.sqlite"

    def for_module(self, name: str) -> Path:
        """Own file for a future module (e.g. ``for_module("memory")``)."""
        if not name or name in {".", ".."} or any(c in name for c in "/\\:"):
            raise ValueError(f"invalid storage module name: {name!r}")
        return self.data_dir / f"{name}.sqlite"

    @property
    def images(self) -> Path:
        """Directory of chat image blobs, one subdirectory per owning private/group scope."""
        return self.data_dir / "images"


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    level: str


SEARCH_ENGINE_NAMES: Final = ("bing", "baidu", "bilibili")


@dataclass(frozen=True, slots=True)
class SearchGeminiConfig:
    """``[search.gemini]``: Gemini google_search grounding via the upstream relay (off by default).

    No dedicated secret: the bot uses ``[provider] api_key``, the sandbox bridge uses
    ``[coding] upstream_api_key`` (falling back to the provider key).
    """

    enabled: bool = False
    base_url: str = ""
    model: str = "gemini-3.1-flash-lite"
    timeout_seconds: float = 90.0


@dataclass(frozen=True, slots=True)
class SearchXConfig:
    """``[search.x]``: X platform search via grok ``x_search`` (Responses API; off by default).

    Falls back to Gemini (X-restricted) when ``[search.gemini]`` is enabled. No dedicated secret:
    the bot uses ``[provider] api_key``.
    """

    enabled: bool = False
    base_url: str = ""
    model: str = "grok-4.7"
    timeout_seconds: float = 90.0


@dataclass(frozen=True, slots=True)
class SearchConfig:
    """``[search]``: web search engines and limits. Cookies are optional secret refs."""

    engines: tuple[str, ...] = SEARCH_ENGINE_NAMES
    default_engine: str = ""  # empty: merge all enabled engines
    timeout_seconds: float = 30.0
    default_count: int = 5
    max_count: int = 10
    baidu_cookies: Secret | None = None
    bilibili_cookies: Secret | None = None
    gemini: SearchGeminiConfig = field(default_factory=lambda: SearchGeminiConfig())
    x: SearchXConfig = field(default_factory=lambda: SearchXConfig())


@dataclass(frozen=True, slots=True)
class MusicConfig:
    """``[music]``: 音乐合成(默认关闭)。合成在 Codex 沙盒内由 ``cgmusic`` 完成。

    ``soundfont_dir`` 是宿主机路径,只读挂载为沙盒 ``/opt/soundfonts``;未配置时由加载器
    取 ``[coding.docker] host_data_dir/soundfonts``(无 coding 时为 ``data_dir/soundfonts``)。
    """

    enabled: bool = False
    soundfont_dir: Path = Path("data/soundfonts")
    muse_dir: Path | None = None  # 可选的宿主机 Muse Sounds 目录;缺省不挂载
    auto_download_soundfont: bool = True  # 启动时后台确保默认音色(失败只记日志)
    auto_render_files: bool = True  # 私聊收到乐谱文件后自动提交一次渲染任务
    max_file_bytes: int = 8 * 1024 * 1024
    skill_ttl_seconds: float = 3600.0
    musescore_enabled: bool = True  # 是否启用 Musescore 工具(搜索/预览,需 music.enabled)及技能说明


@dataclass(frozen=True, slots=True)
class Config:
    napcat: NapCatConfig
    scope: ScopeConfig
    provider: ProviderConfig
    group: GroupConfig
    persona: CharacterCard
    storage: StoragePaths
    logging: LoggingConfig
    voice: VoiceConfig | None = None  # None: [voice] table absent, voice disabled
    coding: CodingConfig | None = None  # None: [coding] absent or enabled = false
    search: SearchConfig = field(default_factory=lambda: SearchConfig())
    music: MusicConfig = field(default_factory=lambda: MusicConfig())
