"""Session compaction (R2.3).

Flow of one compaction (``compact``):
1. Load the active window and expand its retained tail (``expand_window``).
2. Cut it (``choose_cut``): ``expanded[:cut]`` is summarised, the tail is kept verbatim.
3. Ask the provider for a plain-text summary (no tools; reasoning tier ``summary``).
4. Commit one ``compaction`` row carrying the summary and a copy of the tail, but only if no
   row was appended since step 1 (``append_if_last``). Otherwise the result is superseded and
   nothing changes; the new message is handled by the normal turn path.

Failure at any step writes nothing and returns ``failed`` with the exception class name only.
Message bodies, summaries and provider errors are never logged.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from cgbot.agent.history import expand_window, render_items
from cgbot.compaction.policy import (
    DEFAULT_SUMMARY_INSTRUCTIONS,
    IDLE_SECONDS,
    KEEP_EXCHANGES,
    PRIVATE_PREFIX,
    SUMMARY_REQUEST,
    choose_cut,
    estimate_tokens,
    over_threshold,
    transcript,
)
from cgbot.config.settings import ReasoningTiers
from cgbot.provider import (
    ModelProvider,
    ModelRequest,
    ModelResponse,
    TextOutput,
    Usage,
    user_message,
)
from cgbot.provider.tiers import effort_for
from cgbot.scheduler import KIND_COMPACTION_IDLE, SystemEvent
from cgbot.session import Item, Kind

log = logging.getLogger(__name__)

Status = Literal[
    "compacted",
    "below_threshold",
    "nothing_to_compact",
    "busy",
    "in_progress",
    "superseded",
    "failed",
    "not_due",
]
Reason = Literal["threshold", "idle", "manual"]


class CompactionError(Exception):
    """The summary is unusable (empty or incomplete response)."""


class CompactionStore(Protocol):
    async def load_active(self, session_id: str) -> list[Item]: ...

    async def last_activity(self, session_id: str) -> float | None: ...

    async def append_if_last(
        self, session_id: str, expected_last_seq: int, kind: Kind, payload: dict[str, Any]
    ) -> int | None: ...


class IdleScheduler(Protocol):
    def schedule(
        self, kind: str, user_id: str | None, payload: dict[str, Any], due_at: float
    ) -> str: ...

    def reschedule(self, event_id: str, due_at: float) -> bool: ...

    def pending(self) -> list[SystemEvent]: ...


@dataclass(frozen=True, slots=True)
class CompactionOutcome:
    status: Status
    reason: Reason
    seq: int | None = None
    tokens: int | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class _UsageMark:
    input_tokens: int
    through_seq: int


def _private_user(session_id: str) -> str:
    if not session_id.startswith(PRIVATE_PREFIX) or len(session_id) == len(PRIVATE_PREFIX):
        raise ValueError("idle compaction applies to private sessions only")
    return session_id[len(PRIVATE_PREFIX) :]


class Compactor:
    """Compacts one session at a time per instance; safe to share across sessions.

    ``is_busy(session_id)`` must report whether a turn is running for the session; compaction
    never runs during a turn. ``reserved_tokens`` is added to the estimate when no usage record
    exists (system prompt and tool definitions are not part of the window).
    """

    def __init__(
        self,
        provider: ModelProvider,
        store: CompactionStore,
        scheduler: IdleScheduler | None,
        tiers: ReasoningTiers,
        *,
        is_busy: Callable[[str], bool],
        clock: Callable[[], float] = time.time,
        reserved_tokens: int = 0,
        keep_exchanges: int = KEEP_EXCHANGES,
        idle_seconds: float = IDLE_SECONDS,
        summary_instructions: str = DEFAULT_SUMMARY_INSTRUCTIONS,
    ) -> None:
        if keep_exchanges < 1:
            raise ValueError("keep_exchanges must be >= 1")
        if reserved_tokens < 0:
            raise ValueError("reserved_tokens must be >= 0")
        if idle_seconds <= 0:
            raise ValueError("idle_seconds must be > 0")
        self._provider = provider
        self._store = store
        self._scheduler = scheduler
        self._tiers = tiers
        self._is_busy = is_busy
        self._clock = clock
        self._reserved = reserved_tokens
        self._keep = keep_exchanges
        self._idle = idle_seconds
        self._instructions = summary_instructions
        self._usage: dict[str, _UsageMark] = {}
        self._running: set[str] = set()

    def record_usage(self, session_id: str, usage: Usage, through_seq: int) -> None:
        """Remember the provider's input token count for the window ending at ``through_seq``.

        ``through_seq`` is the seq of the newest window row that was in the request. Rows with a
        higher seq are estimated on top of ``usage.input_tokens``. Cleared by compaction.
        """
        self._usage[session_id] = _UsageMark(usage.input_tokens, through_seq)

    def _measure(self, session_id: str, window: list[Item]) -> int:
        mark = self._usage.get(session_id)
        if mark is None:
            return self._reserved + self._estimate(window)
        newer = [item for item in window if item.seq > mark.through_seq]
        return mark.input_tokens + self._estimate(newer)

    @staticmethod
    def _estimate(window: list[Item]) -> int:
        if not window:
            return 0
        return estimate_tokens(render_items(expand_window(window)))

    async def maybe_compact(self, session_id: str) -> CompactionOutcome:
        """Compact if the window reached 50% of the provider's context window."""
        if session_id in self._running:
            return CompactionOutcome("in_progress", "threshold")
        if self._is_busy(session_id):
            return CompactionOutcome("busy", "threshold")
        window_size = self._provider.context_window
        if window_size <= 0:
            raise ValueError("provider context_window must be positive")
        window = await self._store.load_active(session_id)
        tokens = self._measure(session_id, window)
        if not over_threshold(tokens, window_size):
            return CompactionOutcome("below_threshold", "threshold", tokens=tokens)
        return await self._compact(session_id, "threshold", tokens)

    async def compact(self, session_id: str, *, reason: Reason = "manual") -> CompactionOutcome:
        """Compact now, regardless of the threshold (busy and in-progress still apply)."""
        return await self._compact(session_id, reason, None)

    async def _compact(
        self, session_id: str, reason: Reason, tokens: int | None
    ) -> CompactionOutcome:
        if session_id in self._running:
            return CompactionOutcome("in_progress", reason, tokens=tokens)
        if self._is_busy(session_id):
            return CompactionOutcome("busy", reason, tokens=tokens)
        self._running.add(session_id)
        try:
            window = await self._store.load_active(session_id)
            expanded = expand_window(window)
            cut = choose_cut(expanded, self._keep)
            head = expanded[:cut]
            if not any(item.kind != "compaction" for item in head):
                return CompactionOutcome("nothing_to_compact", reason, tokens=tokens)
            summary = await self._summarise(head)
            if self._is_busy(session_id):
                return CompactionOutcome("busy", reason, tokens=tokens)
            payload: dict[str, Any] = {
                "summary": summary,
                "retained": [
                    {"seq": item.seq, "kind": item.kind, "payload": item.payload, "ts": item.ts}
                    for item in expanded[cut:]
                ],
                "through_seq": head[-1].seq,
                "reason": reason,
                "keep_exchanges": self._keep,
            }
            expected = window[-1].seq if window else 0
            seq = await self._store.append_if_last(session_id, expected, "compaction", payload)
            if seq is None:
                return CompactionOutcome("superseded", reason, tokens=tokens)
            self._usage.pop(session_id, None)
            return CompactionOutcome("compacted", reason, seq=seq, tokens=tokens)
        except Exception as exc:
            error = type(exc).__name__
            log.warning("compaction failed (%s): %s", reason, error)
            return CompactionOutcome("failed", reason, tokens=tokens, error=error)
        finally:
            self._running.discard(session_id)

    async def _summarise(self, items: list[Item]) -> str:
        request = ModelRequest(
            instructions=self._instructions,
            input=[user_message(transcript(items) + "\n\n" + SUMMARY_REQUEST)],
            tools=[],
            reasoning_effort=effort_for("summary", self._tiers),
        )
        response = await self._provider.respond(request)
        text = _output_text(response)
        if response.status != "completed" or not text:
            raise CompactionError("empty or incomplete summary")
        return text

    # ---- idle scheduling -------------------------------------------------------------------

    def note_activity(self, session_id: str) -> None:
        """Push the idle compaction of a private session to ``now + idle_seconds``.

        Group sessions are ignored: they never take part in idle compaction.

        Called after each new row. Re-uses the pending event when there is one; creates it
        otherwise (also when the only candidate is currently being delivered).
        """
        if self._scheduler is None or not session_id.startswith(PRIVATE_PREFIX):
            return
        self._arm(session_id, self._clock() + self._idle)

    def _arm(self, session_id: str, due_at: float) -> None:
        sched = self._scheduler
        if sched is None:
            return
        for event in sched.pending():
            if event.kind == KIND_COMPACTION_IDLE and event.payload.get("session_id") == session_id:
                if sched.reschedule(event.event_id, due_at):
                    return
        sched.schedule(
            KIND_COMPACTION_IDLE,
            _private_user(session_id),
            {"session_id": session_id},
            due_at,
        )

    async def handle_idle_event(self, event: SystemEvent) -> CompactionOutcome:
        """Deliver a ``KIND_COMPACTION_IDLE`` event.

        Not idle long enough yet (activity happened after scheduling): re-armed at
        ``last_activity + idle_seconds``. Busy or in progress: re-armed at ``now + idle_seconds``.
        Otherwise compacts once. No further idle event is armed here; the next activity arms it.
        """
        if event.kind != KIND_COMPACTION_IDLE:
            raise ValueError("not a compaction idle event")
        session_id = str(event.payload["session_id"])
        last = await self._store.last_activity(session_id)
        if last is None:
            return CompactionOutcome("not_due", "idle")
        now = self._clock()
        if now < last + self._idle:
            self._arm(session_id, last + self._idle)
            return CompactionOutcome("not_due", "idle")
        if session_id in self._running or self._is_busy(session_id):
            self._arm(session_id, now + self._idle)
            return CompactionOutcome("busy", "idle")
        return await self._compact(session_id, "idle", None)


def _output_text(response: ModelResponse) -> str:
    return "".join(item.text for item in response.items if isinstance(item, TextOutput)).strip()
