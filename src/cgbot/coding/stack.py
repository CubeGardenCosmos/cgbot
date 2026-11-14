"""Assembly of the Coding Agent subsystem for the bot process (M5 R5.4 integration).

``CodingStack`` wires the finished slices together: auth, concurrency governor, queue store,
Docker sandboxes, the Codex app-server manager, the bridge (uploads, reports, files), image
generation, the ``/v1`` gateway web listener, and the ``codex_*`` tool family for the agent.
It owns lifecycle ordering only; no slice logic lives here.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from cgbot.agent.tools import ToolSpec
from cgbot.coding.appserver import connect_codex
from cgbot.coding.auth import HmacSandboxAuth
from cgbot.coding.bridge import BridgeHandle, bridge_routes
from cgbot.coding.config import CodingConfig
from cgbot.coding.contracts import CodingNotifier, CodingService
from cgbot.coding.governor import ConcurrencyGovernor
from cgbot.coding.imagegen import ImageGenService, imagegen_routes
from cgbot.coding.manager import CodingManager
from cgbot.coding.queue_store import QueueStore
from cgbot.coding.sandbox import DockerSandboxProvider
from cgbot.coding.searchbridge import search_routes
from cgbot.coding.tools import codex_tools
from cgbot.coding.web import CodingWeb
from cgbot.config.settings import Secret
from cgbot.dispatch.timebase import Timebase
from cgbot.search import SearchService
from cgbot.search.gemini import GeminiSearchClient

log = logging.getLogger(__name__)

Clock = Callable[[], float]


class CodingStack:
    """One coding subsystem per bot process. Build it only when ``[coding]`` is configured."""

    def __init__(
        self,
        config: CodingConfig,
        *,
        upstream_base_url: str,
        upstream_api_key: Secret,
        notifier: CodingNotifier,
        queue_path: Path,
        clock: Clock = time.time,
        search_service: SearchService | None = None,
        gemini_client: GeminiSearchClient | None = None,
        soundfont_dir: Path | None = None,
        muse_dir: Path | None = None,
        timebase: Timebase | None = None,
    ) -> None:
        auth = HmacSandboxAuth(config.token_secret)
        self._governor = ConcurrencyGovernor(config.concurrency, clock)
        self._store = QueueStore(queue_path)
        self._sandbox = DockerSandboxProvider(
            config, auth, soundfont_dir=soundfont_dir, muse_dir=muse_dir
        )
        self._manager = CodingManager(
            self._sandbox,
            connect_codex,
            notifier,
            self._governor,
            self._store,
            clock,
            time_budget=config.time_budget,
            timebase=timebase,
        )
        self._bridge: BridgeHandle = bridge_routes(config, auth, notifier, clock=clock)
        self._imagegen = ImageGenService(config)
        self._web = CodingWeb(
            config,
            upstream_base_url=upstream_base_url,
            upstream_api_key=upstream_api_key,
            status_hook=self._governor.on_upstream_status,
            extra_routes=[
                *self._bridge.routes,
                *imagegen_routes(self._imagegen, auth),
                *search_routes(search_service, gemini_client, auth),
            ],
        )
        self.tools: tuple[ToolSpec, ...] = codex_tools(self._manager)
        self.service: CodingService = self._manager
        self._started = False

    @property
    def sandbox(self) -> DockerSandboxProvider:
        """Per-user sandbox provider: leases the user container and owns the Docker client."""
        return self._sandbox

    async def start(self) -> None:
        """Start sandboxes, manager recovery, bridge sweeper and the web listener (idempotent).

        A failed step stops whatever already started, then re-raises.
        """
        if self._started:
            return
        try:
            await self._sandbox.start()
            await self._manager.start()
            await self._bridge.start()
            await self._web.start()
        except BaseException:
            await self.stop()
            raise
        self._started = True

    async def stop(self) -> None:
        """Stop in reverse order. Safe without ``start``; each step is isolated from the others."""
        self._started = False
        steps: tuple[tuple[str, Callable[[], Awaitable[object]]], ...] = (
            ("web", self._web.stop),
            ("bridge", self._bridge.close),
            ("manager", self._manager.close),
            ("sandbox", self._sandbox.close),
            ("imagegen", self._imagegen.close),
        )
        for name, step in steps:
            try:
                await step()
            except Exception as exc:
                log.warning("coding %s stop failed: error=%s", name, type(exc).__name__)
