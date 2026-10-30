"""Context compaction for private sessions (R2.3): threshold, summary commit, idle schedule."""

from cgbot.compaction.compactor import (
    CompactionError,
    CompactionOutcome,
    CompactionStore,
    Compactor,
    IdleScheduler,
    Reason,
    Status,
)
from cgbot.compaction.policy import (
    IDLE_SECONDS,
    KEEP_EXCHANGES,
    choose_cut,
    estimate_tokens,
    over_threshold,
)

__all__ = [
    "IDLE_SECONDS",
    "KEEP_EXCHANGES",
    "CompactionError",
    "CompactionOutcome",
    "CompactionStore",
    "Compactor",
    "IdleScheduler",
    "Reason",
    "Status",
    "choose_cut",
    "estimate_tokens",
    "over_threshold",
]
