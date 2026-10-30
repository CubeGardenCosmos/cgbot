"""Offline helpers for coding imagegen tests: config, images, a recording provider transport."""

from __future__ import annotations

import base64
import inspect
import io
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from PIL import Image

from cgbot.coding.config import (
    BridgeConfig,
    CodexConfig,
    CodingConfig,
    ConcurrencyConfig,
    DockerConfig,
    HttpConfig,
    ImageGenConfig,
)
from cgbot.config.settings import Secret

SECRET = "sk-imagegen-test-secret-7f3a"
USER = "alice"
BASE_URL = "https://provider.example/v1"
Responder = Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]]


def make_config(
    root: Path,
    *,
    configured: bool = True,
    mode: str = "chat",
    max_concurrency: int = 2,
) -> CodingConfig:
    imagegen = (
        ImageGenConfig(
            base_url=BASE_URL,
            api_key=Secret(SECRET),
            model="test-image-model",
            mode=mode,
            timeout_seconds=30.0,
            proxy="",
            max_concurrency=max_concurrency,
        )
        if configured
        else None
    )
    return CodingConfig(
        token_secret=Secret("token-secret-test"),
        http=HttpConfig(host="127.0.0.1", port=0, public_base_url="http://127.0.0.1"),
        docker=DockerConfig(
            host="unix:///var/run/docker.sock",
            sandbox_image="sandbox:test",
            network="test-net",
            host_data_dir=root,
            egress_proxy="",
            idle_seconds=60.0,
            container_prefix="cg-",
        ),
        codex=CodexConfig(
            model="test-model", reasoning_effort="low", appserver_port=8080, context_window=128000
        ),
        concurrency=ConcurrencyConfig(
            per_user=1,
            degraded=1,
            degrade_failures=3,
            degrade_window_seconds=60.0,
            recover_after_seconds=60.0,
        ),
        bridge=BridgeConfig(
            max_file_bytes=1024,
            chunk_bytes=512,
            temp_dir=root / "tmp",
            file_ttl_seconds=60.0,
        ),
        imagegen=imagegen,
    )


def workspace(root: Path, user: str = USER) -> Path:
    path = root / "users" / user
    path.mkdir(parents=True, exist_ok=True)
    return path


def output_dir(root: Path, user: str = USER) -> Path:
    return root / "users" / user / "artifacts" / "imagegen"


def png_bytes(width: int, height: int) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(buffer, "PNG")
    return buffer.getvalue()


def jpeg_bytes(width: int, height: int) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (30, 200, 30)).save(buffer, "JPEG")
    return buffer.getvalue()


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def chat_reply(content: Any) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def images_reply(item: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"data": [item]})


class RecordingProvider:
    """Records every request the service sends and answers with ``respond``."""

    def __init__(self, respond: Responder) -> None:
        self.requests: list[httpx.Request] = []
        self._respond = respond

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        result = self._respond(request)
        if inspect.isawaitable(result):
            return await result
        return result

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def posts(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "POST"]

    def gets(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "GET"]


@pytest.fixture(name="owners")
def owner_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int, int]]:
    """Record ownership changes; tests run unprivileged, so chown/fchown must not hit the FS."""
    calls: list[tuple[str, int, int]] = []

    def record_chown(path: Any, uid: int, gid: int, **_: Any) -> None:
        calls.append(("chown", uid, gid))

    def record_fchown(fd: int, uid: int, gid: int) -> None:
        calls.append(("fchown", uid, gid))

    monkeypatch.setattr(os, "chown", record_chown)
    monkeypatch.setattr(os, "fchown", record_fchown)
    return calls
