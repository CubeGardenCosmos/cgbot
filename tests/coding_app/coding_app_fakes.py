"""Offline fakes for ``tests/coding_app``: no Docker, no network, no real Codex or upstream."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from cgbot.agent.tools import ToolContext
from cgbot.coding.config import (
    BridgeConfig,
    CodexConfig,
    CodingConfig,
    ConcurrencyConfig,
    DockerConfig,
    HttpConfig,
    ImageGenConfig,
)
from cgbot.coding.contracts import SubmitResult, ThreadStatus
from cgbot.config import Config, load_config
from cgbot.config.settings import Secret
from cgbot.model import Channel, OutboundMessage, SendResult

Sender = Callable[[OutboundMessage], Awaitable[SendResult]]

IN_SCOPE = "1001"
GROUP_ID = "2001"


class FakeService:
    """Stands in for ``CodingService``. Records every call; ``error`` is raised after recording."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.error: Exception | None = None
        self.submit_result = SubmitResult(thread_id="t1", state="started", queue_position=0)
        self.threads = [
            ThreadStatus(
                thread_id="t1",
                title="论文",
                running=True,
                queued=0,
                last_text="进展",
                updated_at=1.0,
            )
        ]
        self.items: list[dict[str, object]] = [{"type": "agentMessage", "text": "结果"}]
        self.read_cursor: str | None = "c2"
        self.dropped = 2

    def _record(self, name: str, user_id: str, **kwargs: Any) -> None:
        self.calls.append((name, user_id, kwargs))
        if self.error is not None:
            raise self.error

    async def submit(
        self, user_id: str, prompt: str, *, thread_id: str | None = None, title: str | None = None
    ) -> SubmitResult:
        self._record("submit", user_id, prompt=prompt, thread_id=thread_id, title=title)
        return self.submit_result

    async def list_threads(self, user_id: str) -> list[ThreadStatus]:
        self._record("list_threads", user_id)
        return list(self.threads)

    async def peek(self, user_id: str, thread_id: str) -> ThreadStatus:
        self._record("peek", user_id, thread_id=thread_id)
        return self.threads[0]

    async def read(
        self, user_id: str, thread_id: str, *, cursor: str | None = None, limit: int = 20
    ) -> tuple[list[dict[str, object]], str | None]:
        self._record("read", user_id, thread_id=thread_id, cursor=cursor, limit=limit)
        return list(self.items), self.read_cursor

    async def steer(self, user_id: str, thread_id: str, text: str) -> None:
        self._record("steer", user_id, thread_id=thread_id, text=text)

    async def stop(self, user_id: str, thread_id: str, *, clear_queue: bool = False) -> int:
        self._record("stop", user_id, thread_id=thread_id, clear_queue=clear_queue)
        return self.dropped

    async def compact(self, user_id: str, thread_id: str) -> None:
        self._record("compact", user_id, thread_id=thread_id)

    async def rename(self, user_id: str, thread_id: str, title: str) -> None:
        self._record("rename", user_id, thread_id=thread_id, title=title)

    async def archive(self, user_id: str, thread_id: str) -> None:
        self._record("archive", user_id, thread_id=thread_id)

    async def delete(self, user_id: str, thread_id: str) -> None:
        self._record("delete", user_id, thread_id=thread_id)

    async def fork(self, user_id: str, thread_id: str) -> SubmitResult:
        self._record("fork", user_id, thread_id=thread_id)
        return self.submit_result

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None


def tool_ctx(user_id: str = IN_SCOPE, *, group: bool = False) -> ToolContext:
    """A ToolContext for a private chat (default) or the group ``GROUP_ID``."""

    async def sender(message: OutboundMessage) -> SendResult:
        return SendResult(ok=True)

    channel = Channel(kind="qq", account=user_id)
    session_id = f"group:{GROUP_ID}" if group else f"private:{user_id}"
    return ToolContext(
        user_id=user_id,
        session_id=session_id,
        source_channel=channel,
        source_message_id=None,
        sender=sender,
        channels=(channel,),
    )


def coding_config(tmp_path: Path, *, imagegen: bool = False) -> CodingConfig:
    """A fully populated, validated-shape ``CodingConfig``; nothing here touches the network."""
    image = (
        ImageGenConfig(
            base_url="https://img.example/v1",
            api_key=Secret("sk-img-test"),
            model="img-model",
            mode="generations",
            timeout_seconds=600.0,
            proxy="",
            max_concurrency=2,
        )
        if imagegen
        else None
    )
    return CodingConfig(
        token_secret=Secret("tok-test-coding"),
        http=HttpConfig(host="127.0.0.1", port=18080, public_base_url="http://127.0.0.1:18080"),
        docker=DockerConfig(
            host="http://127.0.0.1:2375",
            sandbox_image="cgbot-sandbox:test",
            network="cgbot_test",
            host_data_dir=tmp_path / "host-data",
            egress_proxy="",
            idle_seconds=3600.0,
            container_prefix="cgbot-sbx-",
        ),
        codex=CodexConfig(
            model="test-model",
            reasoning_effort="medium",
            appserver_port=8765,
            context_window=128000,
        ),
        concurrency=ConcurrencyConfig(
            per_user=4,
            degraded=1,
            degrade_failures=3,
            degrade_window_seconds=60.0,
            recover_after_seconds=3600.0,
        ),
        bridge=BridgeConfig(
            max_file_bytes=536870912,
            chunk_bytes=2097152,
            temp_dir=tmp_path / "bridge",
            file_ttl_seconds=86400.0,
        ),
        imagegen=image,
    )


_APP_TOML = """
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

_CODING_TOML = """
[coding]
token_secret = "secret:coding_token"

[coding.http]
host = "127.0.0.1"
port = 18080
public_base_url = "http://127.0.0.1:18080"

[coding.docker]
host = "http://127.0.0.1:2375"
sandbox_image = "cgbot-sandbox:test"
network = "cgbot_test"
host_data_dir = "{host_data}"
egress_proxy = ""
idle_seconds = 3600
container_prefix = "cgbot-sbx-"

[coding.codex]
model = "test-model"
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
temp_dir = "bridge"
file_ttl_seconds = 86400
"""


def app_config(tmp_path: Path, *, coding: bool) -> Config:
    """Load a real ``Config`` through the TOML loader; ``coding`` adds a ``[coding]`` table."""
    secrets = tmp_path / "secrets.toml"
    secrets.write_text(
        '[secrets]\nprovider_api_key = "sk-test-CODING"\ncoding_token = "tok-test-CODING"\n',
        encoding="utf-8",
    )
    secrets.chmod(0o600)
    data_dir = tmp_path / "data"
    config_file = tmp_path / "config.toml"
    extra = _CODING_TOML.format(host_data=tmp_path / "host-data") if coding else ""
    config_file.write_text(
        _APP_TOML + f'\n[storage]\ndata_dir = "{data_dir}"\n' + extra, encoding="utf-8"
    )
    return load_config(config_file, secrets, env={})


class NullNotifier:
    async def notify(self, notice: object) -> None:
        return None


class FakePart:
    """A lifecycle stand-in: records ``<name>.<op>`` into a shared log; raises on fail_on."""

    def __init__(
        self, name: str, events: list[str], fail_on: set[str], *, routes: tuple[object, ...] = ()
    ) -> None:
        self.name = name
        self.events = events
        self.fail_on = fail_on
        self.routes: list[object] = list(routes)
        self.kwargs: dict[str, Any] = {}

    async def _op(self, op: str) -> None:
        key = f"{self.name}.{op}"
        self.events.append(key)
        if key in self.fail_on:
            raise RuntimeError("fake failure")

    async def start(self) -> None:
        await self._op("start")

    async def stop(self) -> None:
        await self._op("stop")

    async def close(self) -> None:
        await self._op("close")
