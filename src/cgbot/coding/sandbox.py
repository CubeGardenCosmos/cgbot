"""Per-user Codex sandbox lifecycle over the Docker Engine API (M5, slice B).

One container per QQ user (``<container_prefix><user_id>``), kept resident while leased and
stopped (never deleted) by the idle reaper. Home and workspace are host bind mounts, so state
survives container recreation. The container is attached only to ``config.docker.network``.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from cgbot.coding.auth import derive_ws_token
from cgbot.coding.config import CodingConfig
from cgbot.coding.contracts import SandboxAuth, SandboxEndpoint, home_dir, workspace_dir
from cgbot.coding.docker import DockerClient, DockerError, NotFound

log = logging.getLogger(__name__)

SANDBOX_UID = 1000
SANDBOX_GID = 1000
LABEL_MANAGED = "cgbot.managed"
LABEL_USER = "cgbot.user"
LABEL_CONFIG_HASH = "cgbot.config-hash"

# Limits copied from the DSH manager (manager/main.py ensure()).
PIDS_LIMIT = 512
MEM_LIMIT_BYTES = 2 * 1024**3
NANO_CPUS = 2_000_000_000
CPU_SHARES = 2
TMPFS_TMP = "rw,exec,nosuid,nodev,size=256m"

_USER_ID = re.compile(r"[0-9]{1,20}")
_PREFIX = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*")


class SandboxError(Exception):
    """Base error for sandbox lifecycle failures."""


class InvalidUserId(SandboxError, ValueError):
    """The user id is not a plain decimal QQ number; it is never put in a container name."""


class SandboxNotReady(SandboxError):
    """app-server did not answer ``/readyz`` within the timeout; the container was stopped."""


class SandboxBusy(SandboxError):
    """stop() was requested while leases are active."""


class UnmanagedContainer(SandboxError):
    """A container already owns the expected name but is not one of ours."""


ReadyProbe = Callable[[str], Awaitable[bool]]


@dataclass(slots=True)
class _UserSandbox:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    leases: int = 0
    running: bool = False
    endpoint: SandboxEndpoint | None = None
    last_used: float = 0.0


class DockerSandboxProvider:
    """Implements :class:`cgbot.coding.contracts.SandboxProvider` on Docker."""

    def __init__(
        self,
        config: CodingConfig,
        auth: SandboxAuth,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        http: httpx.AsyncClient | None = None,
        probe: ReadyProbe | None = None,
        chown: Callable[[str, int, int], None] = os.chown,
        ready_timeout: float = 60.0,
        ready_interval: float = 1.0,
        reap_interval: float = 30.0,
    ) -> None:
        if not _PREFIX.fullmatch(config.docker.container_prefix):
            raise ValueError("docker.container_prefix must be a valid container-name prefix")
        self._config = config
        self._auth = auth
        self._clock = clock
        self._sleep = sleep
        self._chown = chown
        self._ready_timeout = ready_timeout
        self._ready_interval = ready_interval
        self._reap_interval = reap_interval
        self._docker = DockerClient(
            http
            if http is not None
            else httpx.AsyncClient(base_url=config.docker.host, timeout=30.0, trust_env=False)
        )
        self._probe_http = httpx.AsyncClient(timeout=2.0, trust_env=False)
        self._probe: ReadyProbe = probe if probe is not None else self._http_probe
        self._states: dict[str, _UserSandbox] = {}
        self._reaper: asyncio.Task[None] | None = None
        base = config.http.public_base_url.rstrip("/")
        self._gateway_url = f"{base}/v1"
        self._bridge_url = f"{base}/bridge"
        self._bot_host = urlsplit(base).hostname or ""
        self._data_dir = Path(config.docker.host_data_dir)

    # ------------------------------------------------------------------ public API

    def lease(self, user_id: str) -> AbstractAsyncContextManager[SandboxEndpoint]:
        return self._lease(validate_user_id(user_id))

    @asynccontextmanager
    async def _lease(self, user_id: str) -> AsyncIterator[SandboxEndpoint]:
        uid = validate_user_id(user_id)
        state = self._state(uid)
        async with state.lock:
            # The first lease after idle always re-verifies the container (it may have died).
            if state.leases == 0 or state.endpoint is None:
                state.endpoint = await self._ensure(uid, state)
            state.leases += 1
            state.last_used = self._clock()
            endpoint = state.endpoint
        try:
            yield endpoint
        finally:
            state.leases -= 1
            state.last_used = self._clock()

    async def stop(self, user_id: str) -> None:
        uid = validate_user_id(user_id)
        state = self._state(uid)
        async with state.lock:
            if state.leases:
                raise SandboxBusy(f"sandbox for {uid} has {state.leases} active lease(s)")
            await self._stop_locked(uid, state)

    async def start(self) -> None:
        """Adopt running managed containers and start the idle reaper."""
        await self.reconcile()
        self._reaper = asyncio.create_task(self._reap_forever(), name="coding-sandbox-reaper")

    async def close(self) -> None:
        if self._reaper is not None:
            self._reaper.cancel()
            with suppress(asyncio.CancelledError):
                await self._reaper
            self._reaper = None
        await self._docker.aclose()
        await self._probe_http.aclose()

    # ------------------------------------------------------------------ reconcile / reaper

    async def reconcile(self) -> None:
        """Register every running ``cgbot.managed`` container so the reaper governs it."""
        items = await self._docker.list_containers(label=f"{LABEL_MANAGED}=true")
        now = self._clock()
        for item in items:
            labels = item.get("Labels") or {}
            uid = labels.get(LABEL_USER, "")
            names = item.get("Names") or []
            if not _USER_ID.fullmatch(uid) or f"/{self._name(uid)}" not in names:
                log.warning("ignoring managed container with unexpected identity")
                continue
            if item.get("State") != "running":
                continue
            state = self._state(uid)
            state.running = True
            state.last_used = now
            log.info("adopted running sandbox for user %s", uid)

    async def reap_once(self) -> None:
        """Stop every idle sandbox (no leases for ``idle_seconds``); leased ones are skipped."""
        idle = self._config.docker.idle_seconds
        for uid, state in list(self._states.items()):
            if not state.running or state.leases or self._clock() - state.last_used < idle:
                continue
            async with state.lock:
                # Re-check under the lock: a lease may have started while we waited for it.
                if state.leases or not state.running:
                    continue
                if self._clock() - state.last_used < idle:
                    continue
                log.info("stopping idle sandbox for user %s", uid)
                await self._stop_locked(uid, state)

    async def _reap_forever(self) -> None:
        while True:
            await self._sleep(self._reap_interval)
            try:
                await self.reap_once()
            except (DockerError, OSError):
                log.exception("idle reaper pass failed; retrying next interval")

    # ------------------------------------------------------------------ internals

    def _state(self, uid: str) -> _UserSandbox:
        state = self._states.get(uid)
        if state is None:
            state = _UserSandbox(last_used=self._clock())
            self._states[uid] = state
        return state

    def _name(self, uid: str) -> str:
        return f"{self._config.docker.container_prefix}{uid}"

    async def _stop_locked(self, uid: str, state: _UserSandbox) -> None:
        state.endpoint = None
        state.running = False
        try:
            await self._docker.stop_container(self._name(uid))
        except NotFound:
            pass

    async def _ensure(self, uid: str, state: _UserSandbox) -> SandboxEndpoint:
        name = self._name(uid)
        ws_token = derive_ws_token(self._config.token_secret, uid)
        body = self._desired_body(uid, ws_token)
        desired_hash = self._config_hash(body)
        await asyncio.to_thread(self._prepare_host_dirs, uid)

        existing = await self._inspect(name)
        if existing is not None:
            labels = existing.get("Config", {}).get("Labels") or {}
            if labels.get(LABEL_MANAGED) != "true" or labels.get(LABEL_USER) != uid:
                raise UnmanagedContainer(f"container name {name} is not a cgbot sandbox")
            if labels.get(LABEL_CONFIG_HASH) != desired_hash:
                log.info("recreating sandbox for user %s: configuration changed", uid)
                await self._stop_locked(uid, state)
                await self._docker.remove_container(name, force=True)
                existing = None
        if existing is None:
            body["Labels"][LABEL_CONFIG_HASH] = desired_hash
            await self._docker.create_container(name, body)
        if existing is None or existing.get("State", {}).get("Status") != "running":
            await self._docker.start_container(name)
        state.running = True
        await self._wait_ready(uid, name, state)
        return SandboxEndpoint(
            user_id=uid,
            container=name,
            url=f"ws://{name}:{self._config.codex.appserver_port}",
            ws_token=ws_token,
        )

    async def _inspect(self, name: str) -> dict[str, Any] | None:
        try:
            return await self._docker.inspect_container(name)
        except NotFound:
            return None

    async def _wait_ready(self, uid: str, name: str, state: _UserSandbox) -> None:
        url = f"http://{name}:{self._config.codex.appserver_port}/readyz"
        deadline = self._clock() + self._ready_timeout
        while True:
            if await self._probe(url):
                return
            if self._clock() >= deadline:
                break
            await self._sleep(self._ready_interval)
        # Clean up: stop (keep the container and home for diagnosis; next lease restarts it).
        try:
            await self._stop_locked(uid, state)
        except DockerError:
            log.exception("failed to stop sandbox %s after readiness timeout", name)
        raise SandboxNotReady(f"sandbox {name} not ready within {self._ready_timeout:g}s")

    async def _http_probe(self, url: str) -> bool:
        try:
            response = await self._probe_http.get(url)
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    def _prepare_host_dirs(self, uid: str) -> None:
        data = self._data_dir
        for root in (data, data / "users", data / "homes"):
            if root.is_symlink():
                raise SandboxError(f"{root} must not be a symlink")
        for path in (workspace_dir(data, uid), home_dir(data, uid)):
            if path.is_symlink():
                raise SandboxError(f"{path.name} must not be a symlink")
            path.mkdir(parents=True, exist_ok=True)
            self._chown(str(path), SANDBOX_UID, SANDBOX_GID)
            path.chmod(0o700)

    def _env(self, uid: str, ws_token: str) -> list[str]:
        token = self._auth.token_for(uid)
        values: dict[str, str] = {
            "CGBOT_USER_ID": uid,
            "CGBOT_GATEWAY_URL": self._gateway_url,
            "CGBOT_GATEWAY_TOKEN": token,
            "CGBOT_BRIDGE_URL": self._bridge_url,
            "CGBOT_BRIDGE_TOKEN": token,
            "CGBOT_WS_TOKEN": ws_token,
            "CGBOT_APPSERVER_PORT": str(self._config.codex.appserver_port),
            "CGBOT_MODEL": self._config.codex.model,
            "CGBOT_REASONING_EFFORT": self._config.codex.reasoning_effort,
            "CGBOT_CONTEXT_WINDOW": str(self._config.codex.context_window),
        }
        proxy = self._config.docker.egress_proxy
        if proxy:
            values["HTTP_PROXY"] = proxy
            values["HTTPS_PROXY"] = proxy
        values["NO_PROXY"] = ",".join(
            part for part in (self._bot_host, "localhost", "127.0.0.1") if part
        )
        return [f"{key}={value}" for key, value in values.items()]

    def _desired_body(self, uid: str, ws_token: str) -> dict[str, Any]:
        data = self._data_dir
        binds = [
            f"{workspace_dir(data, uid)}:/workspace:rw",
            f"{home_dir(data, uid)}:/home/agent:rw",
        ]
        network = self._config.docker.network
        return {
            "Image": self._config.docker.sandbox_image,
            "User": f"{SANDBOX_UID}:{SANDBOX_GID}",
            "WorkingDir": "/workspace",
            "Env": self._env(uid, ws_token),
            "Labels": {LABEL_MANAGED: "true", LABEL_USER: uid},
            "HostConfig": {
                "Binds": binds,
                "NetworkMode": network,
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges:true"],
                "PidsLimit": PIDS_LIMIT,
                "Memory": MEM_LIMIT_BYTES,
                "NanoCpus": NANO_CPUS,
                "CpuShares": CPU_SHARES,
                "Init": True,
                "RestartPolicy": {"Name": "no"},
                "Tmpfs": {"/tmp": TMPFS_TMP},
            },
            "NetworkingConfig": {"EndpointsConfig": {network: {}}},
        }

    def _config_hash(self, body: dict[str, Any]) -> str:
        """Keyed digest of the desired container spec; a change recreates the container.

        Keyed with the token secret so the label does not expose an unkeyed digest of tokens.
        """
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        key = self._config.token_secret.reveal().encode()
        return hmac.new(key, canonical, hashlib.sha256).hexdigest()


def validate_user_id(user_id: str) -> str:
    """Return ``user_id`` unchanged if it is a plain decimal id; otherwise raise."""
    if not isinstance(user_id, str) or not _USER_ID.fullmatch(user_id):
        raise InvalidUserId("user id must be 1-20 decimal digits")
    return user_id
