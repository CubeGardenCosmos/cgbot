"""Memory book (R2.5): domain-tagged long-term memory with hard visibility filtering.

- :class:`MemoryBook`: async facade over one SQLite file (``StoragePaths.for_module("memory")``).
- :class:`Viewer`: the read/write scope (``Viewer.private(user)`` or ``Viewer.group(gid, users)``).
- :func:`memory_tools`: private-chat tools (``memory_add``, ``memory_search``, ``memory_forget``).
"""

from cgbot.memory.book import MemoryBook
from cgbot.memory.domain import Viewer, group_domain
from cgbot.memory.store import (
    CrossDomainWriteError,
    MemoryBookError,
    MemoryEntry,
    MemoryStoreError,
    format_entries,
)
from cgbot.memory.tools import memory_tools

__all__ = [
    "CrossDomainWriteError",
    "MemoryBook",
    "MemoryBookError",
    "MemoryEntry",
    "MemoryStoreError",
    "Viewer",
    "format_entries",
    "group_domain",
    "memory_tools",
]
