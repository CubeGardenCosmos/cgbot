"""Due-time scheduler and system event queue (R1.7)."""

from cgbot.scheduler.events import (
    KIND_BACKGROUND_NOTICE,
    KIND_COMPACTION_IDLE,
    SystemEvent,
    SystemEventQueue,
)
from cgbot.scheduler.scheduler import (
    DEFAULT_HANDLER_TIMEOUT,
    DEFAULT_MAX_ATTEMPTS,
    FailedEvent,
    Scheduler,
    SchedulerError,
)

__all__ = [
    "KIND_BACKGROUND_NOTICE",
    "KIND_COMPACTION_IDLE",
    "DEFAULT_HANDLER_TIMEOUT",
    "DEFAULT_MAX_ATTEMPTS",
    "FailedEvent",
    "Scheduler",
    "SchedulerError",
    "SystemEvent",
    "SystemEventQueue",
]
