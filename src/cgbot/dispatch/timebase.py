"""Injectable clock for the private dispatcher: monotonic time and cancellable sleep."""

from __future__ import annotations

import asyncio
import time
from typing import Protocol


class Timebase(Protocol):
    def now(self) -> float:
        """Monotonic seconds."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Suspend for ``seconds`` of this timebase."""
        ...


class SystemTimebase:
    """Production clock: ``time.monotonic`` and ``asyncio.sleep``."""

    def now(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
