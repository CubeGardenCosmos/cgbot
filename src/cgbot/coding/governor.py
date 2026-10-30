"""Global Codex concurrency governor (M5 R5.3-T04, DESIGN §13.6b / §13.7).

The per-user limit is ``per_user``. Upstream failures are counted globally: when
``degrade_failures`` failures (429, 5xx, or status 0 = connection failure) occur inside a rolling
``degrade_window_seconds`` window, the limit for every user drops to ``degraded``.

Recovery (推论, see docs/todos/R5.3): the limit returns to ``per_user`` once
``recover_after_seconds`` have elapsed since the quiet point, where the quiet point is the later of
the degradation time and the most recent 429. Only 429s postpone recovery.

Running turns are never killed by degradation; the lower limit only blocks new starts.
"""

from __future__ import annotations

import logging
import math
import time
from collections import deque
from collections.abc import Callable

from cgbot.coding.config import ConcurrencyConfig

logger = logging.getLogger(__name__)

Clock = Callable[[], float]
LimitListener = Callable[[int], None]


def _validate(config: ConcurrencyConfig) -> None:
    if config.per_user < 1:
        raise ValueError("per_user must be >= 1")
    if not 1 <= config.degraded <= config.per_user:
        raise ValueError("degraded must be within [1, per_user]")
    if config.degrade_failures < 1:
        raise ValueError("degrade_failures must be >= 1")
    if not (math.isfinite(config.degrade_window_seconds) and config.degrade_window_seconds > 0):
        raise ValueError("degrade_window_seconds must be a positive finite number")
    if not (math.isfinite(config.recover_after_seconds) and config.recover_after_seconds > 0):
        raise ValueError("recover_after_seconds must be a positive finite number")


class ConcurrencyGovernor:
    """Tracks the effective per-user concurrency limit; thread-unsafe, event-loop only."""

    def __init__(
        self,
        config: ConcurrencyConfig,
        clock: Clock = time.time,
        *,
        on_change: LimitListener | None = None,
    ) -> None:
        _validate(config)
        self._config = config
        self._clock = clock
        # Bounded: only the last `degrade_failures` failures matter for the trigger.
        self._failures: deque[float] = deque(maxlen=config.degrade_failures)
        self._degraded = False
        self._degraded_at = 0.0
        self._last_429: float | None = None
        self._limit = config.per_user
        self._listeners: list[LimitListener] = []
        if on_change is not None:
            self._listeners.append(on_change)

    def add_listener(self, listener: LimitListener) -> None:
        """Register a callback invoked with the new limit whenever it changes."""
        self._listeners.append(listener)

    @property
    def per_user(self) -> int:
        return self._config.per_user

    @property
    def degraded(self) -> bool:
        self._refresh(self._clock())
        return self._degraded

    def limit(self) -> int:
        """Current per-user concurrency limit."""
        self._refresh(self._clock())
        return self._limit

    def recovers_at(self) -> float | None:
        """Absolute time at which the degraded limit recovers, or None when not degraded."""
        now = self._clock()
        self._refresh(now)
        if not self._degraded:
            return None
        return self._quiet_since() + self._config.recover_after_seconds

    def record_failure(self, now: float, *, rate_limited: bool = False) -> None:
        """Record one upstream failure observed at ``now``."""
        self._refresh(now)
        if rate_limited:
            self._last_429 = now if self._last_429 is None else max(self._last_429, now)
        if self._degraded:
            return
        cutoff = now - self._config.degrade_window_seconds
        while self._failures and self._failures[0] < cutoff:
            self._failures.popleft()
        self._failures.append(now)
        if len(self._failures) >= self._config.degrade_failures:
            self._degrade(now)

    def on_upstream_status(self, status: int) -> None:
        """Feed one upstream HTTP status (0 = connection failure) from the gateway."""
        if status == 429:
            self.record_failure(self._clock(), rate_limited=True)
        elif status == 0 or status >= 500:
            self.record_failure(self._clock())

    def _quiet_since(self) -> float:
        if self._last_429 is None:
            return self._degraded_at
        return max(self._degraded_at, self._last_429)

    def _degrade(self, now: float) -> None:
        self._degraded = True
        self._degraded_at = now
        self._failures.clear()
        self._set_limit(self._config.degraded)

    def _refresh(self, now: float) -> None:
        if not self._degraded:
            return
        if now - self._quiet_since() >= self._config.recover_after_seconds:
            self._degraded = False
            self._failures.clear()
            self._set_limit(self._config.per_user)

    def _set_limit(self, new: int) -> None:
        if new == self._limit:
            return
        self._limit = new
        for listener in list(self._listeners):
            try:
                listener(new)
            except Exception as exc:
                logger.warning("concurrency listener failed (%s)", type(exc).__name__)
