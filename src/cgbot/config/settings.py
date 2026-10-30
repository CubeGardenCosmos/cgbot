"""Immutable configuration objects produced by :func:`cgbot.config.load_config`."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Scope = Literal["private", "group"]


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
    """Single model provider (OpenAI Responses compatible upstream)."""

    base_url: str
    model: str
    context_window: int
    api_key: Secret
    reasoning: ReasoningTiers


@dataclass(frozen=True, slots=True)
class GroupConfig:
    """Keyword rules for group chat. Group gating itself lives in later milestones."""

    wake_keywords: tuple[str, ...]
    mute_keywords: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StorageConfig:
    data_dir: Path


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    level: str


@dataclass(frozen=True, slots=True)
class Config:
    napcat: NapCatConfig
    scope: ScopeConfig
    provider: ProviderConfig
    group: GroupConfig
    storage: StorageConfig
    logging: LoggingConfig
