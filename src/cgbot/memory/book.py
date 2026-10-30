"""Async facade over :class:`MemoryStore` (R2.5).

All SQLite work runs in a worker thread via ``asyncio.to_thread``, matching the other stores.
Callers pass a :class:`Viewer`; there is no unscoped read path.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable, Iterable

from cgbot.memory.domain import Viewer, check_id, group_domain
from cgbot.memory.store import (
    DEFAULT_IMPORTANCE,
    DEFAULT_SEARCH_LIMIT,
    MemoryEntry,
    MemoryStore,
    SearchBackend,
)


class MemoryBook:
    """Persistent memory book backed by one SQLite file (``StoragePaths.for_module("memory")``)."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        clock: Callable[[], float] = time.time,
        use_fts: bool | None = None,
    ) -> None:
        self._store = MemoryStore(path, clock=clock, use_fts=use_fts)

    @property
    def search_backend(self) -> SearchBackend:
        return self._store.search_backend

    async def add(
        self,
        *,
        subject_user: str,
        domain: str,
        source_domain: str,
        text: str,
        tags: Iterable[str] | None = None,
        importance: int = DEFAULT_IMPORTANCE,
    ) -> MemoryEntry:
        """Store an entry. ``domain`` must equal ``source_domain`` (else CrossDomainWriteError)."""
        return await asyncio.to_thread(
            self._store.add,
            subject_user=subject_user,
            domain=domain,
            source_domain=source_domain,
            text=text,
            tags=tags,
            importance=importance,
        )

    async def add_group_profile(
        self,
        group_id: str,
        user_id: str,
        text: str,
        *,
        tags: Iterable[str] | None = None,
        importance: int = DEFAULT_IMPORTANCE,
    ) -> MemoryEntry:
        """Group speaker-profile entry (R3.5 boundary): domain and source are ``group:<id>``.

        The entry is readable only by a group viewer of the same group, and never by a private
        viewer. The caller supplies the group id from the group session it is processing.
        """
        domain = group_domain(group_id)
        return await self.add(
            subject_user=check_id(user_id, "user id"),
            domain=domain,
            source_domain=domain,
            text=text,
            tags=tags,
            importance=importance,
        )

    async def get(self, entry_id: int, *, viewer: Viewer) -> MemoryEntry | None:
        return await asyncio.to_thread(self._store.get, entry_id, viewer)

    async def update(
        self,
        entry_id: int,
        *,
        viewer: Viewer,
        text: str | None = None,
        tags: Iterable[str] | None = None,
        importance: int | None = None,
    ) -> MemoryEntry | None:
        return await asyncio.to_thread(
            lambda: self._store.update(
                entry_id, viewer=viewer, text=text, tags=tags, importance=importance
            )
        )

    async def delete(self, entry_id: int, *, viewer: Viewer) -> bool:
        return await asyncio.to_thread(self._store.delete, entry_id, viewer=viewer)

    async def search(
        self,
        query: str,
        *,
        viewer: Viewer,
        user_filter: Iterable[str] | None = None,
        limit: int = DEFAULT_SEARCH_LIMIT,
    ) -> list[MemoryEntry]:
        """Visibility filter first, then ranking over visible rows only."""
        materialised = None if user_filter is None else tuple(user_filter)
        return await asyncio.to_thread(
            lambda: self._store.search(query, viewer=viewer, user_filter=materialised, limit=limit)
        )

    async def list_for_prompt(
        self,
        *,
        viewer: Viewer,
        budget_chars: int,
        user_filter: Iterable[str] | None = None,
    ) -> list[MemoryEntry]:
        """Priority prefix of visible entries that fits ``budget_chars`` (see store docs)."""
        materialised = None if user_filter is None else tuple(user_filter)
        return await asyncio.to_thread(
            lambda: self._store.list_for_prompt(
                viewer=viewer, budget_chars=budget_chars, user_filter=materialised
            )
        )

    async def close(self) -> None:
        await asyncio.to_thread(self._store.close)
