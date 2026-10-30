"""Immutable configuration objects produced by :func:`cgbot.config.load_config`."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

if TYPE_CHECKING:
    # voice/config.py imports ConfigError from this module; a runtime import would be a cycle.
    from cgbot.voice.config import VoiceConfig

Scope = Literal["private", "group"]
WireApi = Literal["responses", "chat"]
WIRE_APIS: Final = ("responses", "chat")


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

    def allows(self, scope: Scope, user_id: str, group_id: str | None) -> bool:
        """Private: sender must be a configured friend. Group: group must be configured.

        Group membership is not friend-gated: any member of a configured group is in scope.
        ``owner`` is an identity marker only and does not extend the allowlist.
        """
        if scope == "private":
            return user_id in self.friends
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
class ProviderConfig:
    """Single model provider; ``wire_api`` selects Responses or Chat Completions on the upstream."""

    base_url: str
    model: str
    context_window: int
    api_key: Secret
    reasoning: ReasoningTiers
    wire_api: WireApi = "responses"  # wire format: Responses (default) or Chat Completions


@dataclass(frozen=True, slots=True)
class GroupConfig:
    """Group chat settings: keyword rules, session lifetime, and group skill-card limits."""

    wake_keywords: tuple[str, ...]
    mute_keywords: tuple[str, ...]
    idle_timeout_seconds: float = 600.0  # [推论] group session ends after this much quiet
    max_turns: int = 30  # [推论] group session ends after this many turns
    skill_topics: int = 8  # [推论] skill-card limits (SkillLimits)
    skill_memes: int = 10
    skill_rules: int = 10
    skill_members: int = 20
    skill_habits_per_member: int = 5


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


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    level: str


@dataclass(frozen=True, slots=True)
class Config:
    napcat: NapCatConfig
    scope: ScopeConfig
    provider: ProviderConfig
    group: GroupConfig
    storage: StoragePaths
    logging: LoggingConfig
    voice: VoiceConfig | None = None  # None: [voice] table absent, voice disabled
