"""CodingWeb: the aiohttp server that hosts the Responses gateway, the bridge and other slices.

Owns one upstream ``ClientSession`` for the gateway and the ``AppRunner``. Other slices contribute
routes through ``extra_routes`` (bridge E, image generation F); their handlers run under the same
listener, so every route shares the same host/port and shutdown path.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Final

import aiohttp
from aiohttp import web

from cgbot.coding.auth import HmacSandboxAuth
from cgbot.coding.config import CodingConfig
from cgbot.coding.contracts import UpstreamStatusHook
from cgbot.coding.gateway import CONNECT_TIMEOUT_SECONDS, IDLE_TIMEOUT_SECONDS, ResponsesGateway
from cgbot.config.settings import Secret

log = logging.getLogger(__name__)

# Bodies are streamed by the handlers, so aiohttp's default 1 MiB read limit only gets in the way
# of large uploads. The bridge enforces its own file-size limit.
CLIENT_MAX_SIZE: Final = 1 << 40
SHUTDOWN_TIMEOUT_SECONDS: Final = 5.0


async def _healthz(_request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


class CodingWeb:
    """Start/stop wrapper around one aiohttp listener for the coding subsystem.

    ``upstream_base_url`` and ``upstream_api_key`` come from ``[provider]``: the gateway forwards
    to that fixed destination and substitutes the key for every sandbox request.
    """

    def __init__(
        self,
        config: CodingConfig,
        *,
        upstream_base_url: str,
        upstream_api_key: Secret,
        status_hook: UpstreamStatusHook,
        extra_routes: Iterable[web.RouteDef] = (),
    ) -> None:
        self._config = config
        self._auth = HmacSandboxAuth(config.token_secret)
        self._upstream_base_url = upstream_base_url
        self._upstream_api_key = upstream_api_key
        self._status_hook = status_hook
        self._extra_routes = tuple(extra_routes)
        self._session: aiohttp.ClientSession | None = None
        self._runner: web.AppRunner | None = None
        self._port: int | None = None

    @property
    def port(self) -> int:
        if self._port is None:
            raise RuntimeError("CodingWeb is not running")
        return self._port

    async def start(self) -> None:
        if self._runner is not None:
            raise RuntimeError("CodingWeb is already running")
        session = aiohttp.ClientSession(
            auto_decompress=False,
            timeout=aiohttp.ClientTimeout(
                total=None,
                connect=CONNECT_TIMEOUT_SECONDS,
                sock_connect=CONNECT_TIMEOUT_SECONDS,
                sock_read=IDLE_TIMEOUT_SECONDS,
            ),
        )
        gateway = ResponsesGateway(
            auth=self._auth,
            session=session,
            upstream_base_url=self._upstream_base_url,
            upstream_api_key=self._upstream_api_key,
            status_hook=self._status_hook,
        )
        app = web.Application(client_max_size=CLIENT_MAX_SIZE)
        app.router.add_get("/healthz", _healthz)
        app.router.add_routes(gateway.routes())
        app.router.add_routes(self._extra_routes)
        # handler_cancellation: a client disconnect cancels the handler, which closes the upstream
        # request, so an abandoned generation does not keep burning upstream tokens.
        # auto_decompress=False: request bodies are forwarded byte-for-byte; the server must not
        # decode Content-Encoding (zstd is not available in the image).
        runner = web.AppRunner(
            app,
            handler_cancellation=True,
            shutdown_timeout=SHUTDOWN_TIMEOUT_SECONDS,
            auto_decompress=False,
        )
        await runner.setup()
        site = web.TCPSite(runner, self._config.http.host, self._config.http.port)
        try:
            await site.start()
        except BaseException:
            await runner.cleanup()
            await session.close()
            raise
        sockname = runner.addresses[0]
        self._session = session
        self._runner = runner
        self._port = int(sockname[1])
        log.info("coding web listening on %s:%d", self._config.http.host, self._port)

    async def stop(self) -> None:
        runner, session = self._runner, self._session
        self._runner = None
        self._session = None
        self._port = None
        try:
            if runner is not None:
                await runner.cleanup()
        finally:
            if session is not None:
                await session.close()
