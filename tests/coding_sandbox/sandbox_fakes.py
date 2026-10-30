"""Offline doubles for the sandbox lifecycle tests: a fake Docker Engine, clock, probe, chown."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from cgbot.coding.config import (
    BridgeConfig,
    CodexConfig,
    CodingConfig,
    ConcurrencyConfig,
    DockerConfig,
    HttpConfig,
    ImageGenConfig,
)
from cgbot.coding.sandbox import DockerSandboxProvider
from cgbot.config.settings import Secret

TOKEN_SECRET = "test-token-secret-value"
UPSTREAM_KEY = "UPSTREAM-IMAGEGEN-KEY-VALUE"
PROXY = "http://egress.test:3128"
NETWORK = "cgbot_test_net"


def make_config(
    tmp_path: Path,
    *,
    idle_seconds: float = 600.0,
    model: str = "gpt-test",
    with_imagegen: bool = True,
) -> CodingConfig:
    imagegen = (
        ImageGenConfig(
            base_url="https://images.upstream.test",
            api_key=Secret(UPSTREAM_KEY),
            model="image-test",
            mode="generations",
            timeout_seconds=30.0,
            proxy="",
            max_concurrency=1,
        )
        if with_imagegen
        else None
    )
    return CodingConfig(
        token_secret=Secret(TOKEN_SECRET),
        http=HttpConfig(host="0.0.0.0", port=8080, public_base_url="http://cgbot-bot.test:8080"),
        docker=DockerConfig(
            host="http://docker-proxy.test:2375",
            sandbox_image="cgbot-sandbox:test",
            network=NETWORK,
            host_data_dir=tmp_path / "data",
            egress_proxy=PROXY,
            idle_seconds=idle_seconds,
            container_prefix="cgbot-sbx-",
        ),
        codex=CodexConfig(
            model=model, reasoning_effort="medium", appserver_port=8765, context_window=128000
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
            chunk_bytes=256,
            temp_dir=tmp_path / "tmp",
            file_ttl_seconds=60.0,
        ),
        imagegen=imagegen,
    )


class FakeDockerd:
    """In-memory Docker Engine subset. Unknown routes fail the test loudly."""

    def __init__(self) -> None:
        self.containers: dict[str, dict[str, Any]] = {}
        self.requests: list[tuple[str, str]] = []
        self.create_bodies: list[dict[str, Any]] = []
        self.starts: list[str] = []
        self.stops: list[str] = []
        self.removes: list[str] = []

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url="http://docker-proxy.test:2375",
            transport=httpx.MockTransport(self.handle),
            trust_env=False,
        )

    def seed(
        self,
        name: str,
        *,
        labels: dict[str, str],
        status: str = "running",
        env: list[str] | None = None,
    ) -> None:
        self.containers[name] = {
            "Id": f"id-{name}",
            "Names": [f"/{name}"],
            "State": {"Status": status},
            "Config": {"Labels": dict(labels), "Env": list(env or [])},
            "HostConfig": {},
        }

    def status(self, name: str) -> str | None:
        container = self.containers.get(name)
        return None if container is None else container["State"]["Status"]

    def handle(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        self.requests.append((method, path))
        if method == "GET" and path == "/containers/json":
            return self._list(request)
        if method == "POST" and path == "/containers/create":
            return self._create(request)
        if method == "GET" and (match := re.fullmatch(r"/containers/([^/]+)/json", path)):
            return self._inspect(match.group(1))
        if method == "POST" and (match := re.fullmatch(r"/containers/([^/]+)/(start|stop)", path)):
            return self._transition(match.group(1), match.group(2))
        if method == "DELETE" and (match := re.fullmatch(r"/containers/([^/]+)", path)):
            return self._remove(
                match.group(1),
                force=parse_qs(request.url.query.decode()).get("force", ["false"])[0] == "true",
            )
        raise AssertionError(f"unexpected docker API call: {method} {path}")

    def _list(self, request: httpx.Request) -> httpx.Response:
        filters = json.loads(parse_qs(request.url.query.decode()).get("filters", ["{}"])[0])
        wanted = filters.get("label", [])
        items = []
        for container in self.containers.values():
            labels = container["Config"]["Labels"]
            if all(_label_matches(labels, spec) for spec in wanted):
                items.append(
                    {
                        "Id": container["Id"],
                        "Names": container["Names"],
                        "State": container["State"]["Status"],
                        "Labels": labels,
                    }
                )
        return _json(200, items)

    def _create(self, request: httpx.Request) -> httpx.Response:
        name = parse_qs(urlsplit(str(request.url)).query)["name"][0]
        body = json.loads(request.content)
        self.create_bodies.append(body)
        if name in self.containers:
            return _json(409, {"message": "Conflict. The container name is already in use"})
        self.containers[name] = {
            "Id": f"id-{name}",
            "Names": [f"/{name}"],
            "State": {"Status": "created"},
            "Config": {"Labels": dict(body.get("Labels") or {}), "Env": body.get("Env", [])},
            "HostConfig": body.get("HostConfig", {}),
        }
        return _json(201, {"Id": f"id-{name}", "Warnings": []})

    def _inspect(self, name: str) -> httpx.Response:
        container = self.containers.get(name)
        if container is None:
            return _json(404, {"message": "No such container"})
        return _json(
            200,
            {
                "Id": container["Id"],
                "Name": f"/{name}",
                "State": {"Status": container["State"]["Status"]},
                "Config": container["Config"],
                "HostConfig": container["HostConfig"],
            },
        )

    def _transition(self, name: str, action: str) -> httpx.Response:
        container = self.containers.get(name)
        if container is None:
            return _json(404, {"message": "No such container"})
        running = container["State"]["Status"] == "running"
        if action == "start":
            self.starts.append(name)
            if running:
                return httpx.Response(304)
            container["State"]["Status"] = "running"
            return httpx.Response(204)
        self.stops.append(name)
        if not running:
            return httpx.Response(304)
        container["State"]["Status"] = "exited"
        return httpx.Response(204)

    def _remove(self, name: str, *, force: bool) -> httpx.Response:
        container = self.containers.get(name)
        if container is None:
            return _json(404, {"message": "No such container"})
        if container["State"]["Status"] == "running" and not force:
            return _json(409, {"message": "cannot remove a running container"})
        self.removes.append(name)
        del self.containers[name]
        return httpx.Response(204)


def _label_matches(labels: dict[str, str], spec: str) -> bool:
    key, _, value = spec.partition("=")
    return labels.get(key) == value


def _json(status: int, payload: Any) -> httpx.Response:
    return httpx.Response(status, json=payload)


class FakeClock:
    """Monotonic clock whose ``sleep`` advances time instead of waiting."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


class FakeReadyProbe:
    """Answers the readiness probe; containers in ``never`` never become ready."""

    def __init__(self, *, ready_after: int = 0, never: frozenset[str] = frozenset()) -> None:
        self._ready_after = ready_after
        self._never = never
        self._counts: dict[str, int] = {}
        self.urls: list[str] = []

    async def __call__(self, url: str) -> bool:
        self.urls.append(url)
        name = urlsplit(url).hostname or ""
        count = self._counts.get(name, 0) + 1
        self._counts[name] = count
        if name in self._never:
            return False
        return count > self._ready_after


class ChownRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, int]] = []

    def __call__(self, path: str, uid: int, gid: int) -> None:
        self.calls.append((path, uid, gid))


def make_provider(
    config: CodingConfig,
    dockerd: FakeDockerd,
    *,
    clock: FakeClock | None = None,
    probe: FakeReadyProbe | None = None,
    chown: ChownRecorder | None = None,
    ready_timeout: float = 60.0,
) -> DockerSandboxProvider:
    from cgbot.coding.auth import HmacSandboxAuth

    fake_clock = clock if clock is not None else FakeClock()
    return DockerSandboxProvider(
        config,
        HmacSandboxAuth(config.token_secret),
        clock=fake_clock,
        sleep=fake_clock.sleep,
        http=dockerd.client(),
        probe=probe if probe is not None else FakeReadyProbe(),
        chown=chown if chown is not None else ChownRecorder(),
        ready_timeout=ready_timeout,
        ready_interval=1.0,
        reap_interval=30.0,
    )
