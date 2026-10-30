"""Shared fakes for the bridge tests (slice E)."""

from __future__ import annotations

from pathlib import Path

from cgbot.coding.config import (
    BridgeConfig,
    CodexConfig,
    CodingConfig,
    ConcurrencyConfig,
    DockerConfig,
    HttpConfig,
)
from cgbot.coding.contracts import CodingNotice
from cgbot.config.settings import Secret

PUBLIC_BASE = "http://bot.test"
MiB = 1024 * 1024


class FakeAuth:
    """Maps bearer tokens to user ids; ``token_for`` derives a token from the user id."""

    def __init__(self, tokens: dict[str, str]) -> None:
        self._tokens = dict(tokens)

    def token_for(self, user_id: str) -> str:
        return f"tok-{user_id}"

    def resolve(self, token: str) -> str | None:
        return self._tokens.get(token)


class FakeNotifier:
    def __init__(self, *, fail: bool = False) -> None:
        self.notices: list[CodingNotice] = []
        self.fail = fail

    async def notify(self, notice: CodingNotice) -> None:
        if self.fail:
            raise RuntimeError("delivery failed; body=SECRET-BODY-TEXT")
        self.notices.append(notice)


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def make_config(
    temp_dir: Path,
    *,
    max_file_bytes: int = 512 * MiB,
    chunk_bytes: int = 2 * MiB,
    file_ttl_seconds: float = 3600.0,
) -> CodingConfig:
    return CodingConfig(
        token_secret=Secret("test-secret"),
        http=HttpConfig(host="127.0.0.1", port=0, public_base_url=PUBLIC_BASE),
        docker=DockerConfig(
            host="unix:///nonexistent",
            sandbox_image="sandbox:test",
            network="test-net",
            host_data_dir=temp_dir,
            egress_proxy="",
            idle_seconds=60.0,
            container_prefix="cgbot-test-",
        ),
        codex=CodexConfig(model="test-model", reasoning_effort="low", appserver_port=8765),
        concurrency=ConcurrencyConfig(
            per_user=1,
            degraded=1,
            degrade_failures=3,
            degrade_window_seconds=60.0,
            recover_after_seconds=60.0,
        ),
        bridge=BridgeConfig(
            max_file_bytes=max_file_bytes,
            chunk_bytes=chunk_bytes,
            temp_dir=temp_dir,
            file_ttl_seconds=file_ttl_seconds,
        ),
        imagegen=None,
    )
