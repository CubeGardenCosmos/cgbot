"""omp_* tool family (R5.7): link and control external ompweb servers from private chat.

Public surface: :class:`OmpStore` (SQLite, mode 0600), :class:`OmpService` (operations,
watchers, reports, wakes), and :func:`omp_tools` (the 15 ToolSpecs bound to a service).
"""

from __future__ import annotations

from cgbot.agent.tools import ToolSpec
from cgbot.omp.client import ClientFactory, default_client_factory
from cgbot.omp.service import OmpService, WakeHandler
from cgbot.omp.store import OmpStore


def omp_tools(
    store: OmpStore,
    *,
    http_client_factory: ClientFactory = default_client_factory,
    on_wake: WakeHandler | None = None,
) -> list[ToolSpec]:
    """Build the omp_* tools over a new :class:`OmpService`.

    The caller owns the service lifecycle: ``await service.start()`` before use and
    ``await service.close()`` on shutdown. This function only returns the tools, so use
    :class:`OmpService` directly when the lifecycle must be explicit.
    """
    service = OmpService(store, http_client_factory=http_client_factory, on_wake=on_wake)
    return service.tools()


__all__ = ["OmpService", "OmpStore", "omp_tools"]
