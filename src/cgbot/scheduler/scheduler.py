"""Persistent due-time scheduler for system events (R1.7).

- Events live in their own SQLite file (``scheduler.sqlite`` under the storage data dir; round
  20 forbids sharing a database between modules). A pending event survives restart; a
  delivered, cancelled or failed event is never delivered again.
- Delivery is at-least-once: an event is marked delivered only after its handler returns. If
  the handler fails, is cancelled by ``stop()``, or the process dies first, the event stays
  pending and the handler may see it again.
- Each attempt is bounded: the handler runs inside ``asyncio.timeout(handler_timeout)``
  (default 30 s, per round 20) and a timeout counts as a failed attempt.
- At most ``max_attempts`` attempts (default 5, per round 20) are made per event. Once they are
  used up the event becomes ``failed``: it stays in the table with the attempt count and the
  last error type name, stops blocking later events, is reported through the optional
  ``on_dead`` callback ``(SystemEvent, error_type_name)`` exactly once, and is logged by
  identifiers only (never the payload). Exhausted events never come back after a restart.
- Between attempts the scheduler waits with exponential backoff capped at
  ``retry_max_delay``; the delay is stored as ``next_attempt_at`` on the event, so a
  backing-off event does not delay other events that are already due. The loop always picks
  the pending event with the earliest ``max(due_at, next_attempt_at)`` (ties: insertion order),
  so only genuinely-not-ready work waits.
- Deliveries are serial; the loop never polls: it sleeps until the next event is ready and is
  woken early by ``schedule``, ``cancel`` and ``reschedule``.
- Due times are absolute seconds on ``clock`` (default ``time.time``). Overdue events, including
  those left over from before a restart, are delivered as soon as the loop runs.
- The handler runs inside the loop and deliveries are serial. A handler should hand the event
  off quickly (e.g. to the owning session's turn dispatcher) instead of awaiting a whole agent
  turn, or every later event waits behind it (up to ``handler_timeout``).
- Methods are called from the event-loop thread; each SQLite call is a short synchronous
  transaction.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cgbot.scheduler.events import SystemEvent

logger = logging.getLogger(__name__)

SCHEMA_COMPONENT = "scheduler"
SCHEMA_VERSION = 2
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_HANDLER_TIMEOUT = 30.0
DEFAULT_RETRY_BASE_DELAY = 1.0
DEFAULT_RETRY_MAX_DELAY = 300.0
_TASK_NAME = "cgbot-scheduler"
_MAX_BACKOFF_EXPONENT = 32
_STATUSES = "'pending', 'delivered', 'cancelled', 'failed'"

Handler = Callable[[SystemEvent], Awaitable[None]]
DeadHandler = Callable[[SystemEvent, str], Awaitable[None] | None]
Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]


class SchedulerError(Exception):
    """Misuse (start twice, close while running) or an incompatible schema."""


async def _default_sleep(delay: float) -> None:
    await asyncio.sleep(delay)


@dataclass(frozen=True, slots=True)
class FailedEvent:
    """An event that used up its attempts. Kept for inspection; never delivered again."""

    event: SystemEvent
    attempts: int
    last_error: str


@dataclass(frozen=True, slots=True)
class _Head:
    event: SystemEvent
    attempts: int
    ready_at: float


def _check_kind(kind: object) -> str:
    if not isinstance(kind, str) or not kind:
        raise ValueError("event kind must be a non-empty str")
    return kind


def _check_user(user_id: object) -> str | None:
    if user_id is None:
        return None
    if not isinstance(user_id, str) or not user_id:
        raise ValueError("user_id must be None or a non-empty str")
    return user_id


def _check_time(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError("due_at must be a real number of seconds")
    due = float(value)
    if not math.isfinite(due):
        raise ValueError("due_at must be finite")
    return due


def _dump(payload: object) -> str:
    if not isinstance(payload, dict):
        raise TypeError("payload must be a dict")
    try:
        return json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise TypeError("payload must be JSON-serializable") from exc


def _load(text: str, event_id: str) -> dict[str, Any]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SchedulerError(f"corrupt payload for event {event_id}") from exc
    if not isinstance(payload, dict):
        raise SchedulerError(f"corrupt payload for event {event_id}")
    return payload


def _event(row: tuple[Any, ...]) -> SystemEvent:
    event_id = str(row[0])
    return SystemEvent(
        event_id=event_id,
        kind=str(row[1]),
        user_id=None if row[2] is None else str(row[2]),
        payload=_load(str(row[3]), event_id),
        due_at=float(row[4]),
    )


def _log_loop_exit(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("scheduler loop stopped by error: %s", type(exc).__name__)


class Scheduler:
    """Persistent event scheduler; see the module docstring for delivery semantics.

    ``sleep`` and ``clock`` are injectable so tests can drive time without real waiting.
    ``max_attempts`` bounds the attempts per event (a timed-out attempt counts); after that the
    event is marked failed and reported to ``on_dead`` once. ``handler_timeout`` bounds each
    attempt. ``retry_base_delay`` / ``retry_max_delay`` bound the backoff between attempts.
    """

    def __init__(
        self,
        db_path: str | os.PathLike[str],
        handler: Handler,
        clock: Clock = time.time,
        *,
        sleep: Sleep = _default_sleep,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        handler_timeout: float = DEFAULT_HANDLER_TIMEOUT,
        retry_base_delay: float = DEFAULT_RETRY_BASE_DELAY,
        retry_max_delay: float = DEFAULT_RETRY_MAX_DELAY,
        on_dead: DeadHandler | None = None,
    ) -> None:
        if not (retry_base_delay > 0 and retry_max_delay >= retry_base_delay):
            raise ValueError("need 0 < retry_base_delay <= retry_max_delay")
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
            raise TypeError("max_attempts must be an int")
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if not (handler_timeout > 0 and math.isfinite(handler_timeout)):
            raise ValueError("handler_timeout must be a positive number of seconds")
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._handler = handler
        self._clock = clock
        self._sleep = sleep
        self._max_attempts = max_attempts
        self._handler_timeout = float(handler_timeout)
        self._retry_base = retry_base_delay
        self._retry_max = retry_max_delay
        self._on_dead = on_dead
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            path, timeout=5.0, isolation_level=None, check_same_thread=False
        )
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._init_schema()
        except BaseException:
            self._conn.close()
            raise
        self._wakeup = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._inflight: str | None = None

    def _init_schema(self) -> None:
        with self._lock, self._transaction() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version "
                "(component TEXT PRIMARY KEY, version INTEGER NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS scheduled_events ("
                "seq INTEGER PRIMARY KEY AUTOINCREMENT, "
                "event_id TEXT NOT NULL UNIQUE, "
                "kind TEXT NOT NULL, "
                "user_id TEXT, "
                "payload TEXT NOT NULL, "
                f"due_at REAL NOT NULL, status TEXT NOT NULL CHECK (status IN ({_STATUSES})), "
                "attempts INTEGER NOT NULL DEFAULT 0, "
                "next_attempt_at REAL NOT NULL DEFAULT 0, "
                "last_error TEXT, "
                "created_at REAL NOT NULL, "
                "updated_at REAL NOT NULL)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS scheduled_events_due "
                "ON scheduled_events (status, due_at, next_attempt_at, seq)"
            )
            conn.execute(
                "INSERT INTO schema_version (component, version) VALUES (?, ?) "
                "ON CONFLICT (component) DO NOTHING",
                (SCHEMA_COMPONENT, SCHEMA_VERSION),
            )
            row = conn.execute(
                "SELECT version FROM schema_version WHERE component = ?", (SCHEMA_COMPONENT,)
            ).fetchone()
            version = int(row[0])
            if version > SCHEMA_VERSION:
                raise SchedulerError(
                    f"scheduler schema version {version} is newer than supported {SCHEMA_VERSION}"
                )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    # ----- registration -------------------------------------------------------------

    def schedule(
        self, kind: str, user_id: str | None, payload: dict[str, Any], due_at: float
    ) -> str:
        """Persist a pending event and return its id. A past ``due_at`` is delivered at once."""
        kind = _check_kind(kind)
        user = _check_user(user_id)
        text = _dump(payload)
        due = _check_time(due_at)
        event_id = uuid.uuid4().hex
        with self._lock, self._transaction() as conn:
            now = self._clock()
            conn.execute(
                "INSERT INTO scheduled_events (event_id, kind, user_id, payload, due_at, "
                "status, attempts, next_attempt_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'pending', 0, 0, ?, ?)",
                (event_id, kind, user, text, due, now, now),
            )
        self._wakeup.set()
        return event_id

    def cancel(self, event_id: str) -> bool:
        """Cancel a pending event.

        Returns False if the id is unknown, the event is already delivered, cancelled or failed,
        or its handler is running right now (a running delivery cannot be withdrawn).
        """
        if event_id == self._inflight:
            return False
        with self._lock, self._transaction() as conn:
            cur = conn.execute(
                "UPDATE scheduled_events SET status = 'cancelled', updated_at = ? "
                "WHERE event_id = ? AND status = 'pending'",
                (self._clock(), event_id),
            )
            changed = cur.rowcount == 1
        if changed:
            self._wakeup.set()
        return changed

    def reschedule(self, event_id: str, due_at: float) -> bool:
        """Move a pending event to a new due time (e.g. idle timers reset by activity).

        Same return contract as ``cancel``: False when the event is not pending or is running.
        """
        due = _check_time(due_at)
        if event_id == self._inflight:
            return False
        with self._lock, self._transaction() as conn:
            cur = conn.execute(
                "UPDATE scheduled_events SET due_at = ?, updated_at = ? "
                "WHERE event_id = ? AND status = 'pending'",
                (due, self._clock(), event_id),
            )
            changed = cur.rowcount == 1
        if changed:
            self._wakeup.set()
        return changed

    def pending(self) -> list[SystemEvent]:
        """Pending events in delivery order ``(due_at, insertion)``.

        Failed events are not pending: they used up their attempts and never run again.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT event_id, kind, user_id, payload, due_at FROM scheduled_events "
                "WHERE status = 'pending' ORDER BY due_at, seq"
            ).fetchall()
        return [_event(r) for r in rows]

    def failed(self) -> list[FailedEvent]:
        """Dead events (attempts exhausted) in ``(due_at, insertion)`` order, with last error."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT event_id, kind, user_id, payload, due_at, attempts, last_error "
                "FROM scheduled_events WHERE status = 'failed' ORDER BY due_at, seq"
            ).fetchall()
        return [
            FailedEvent(
                event=_event(row[:5]),
                attempts=int(row[5]),
                last_error="" if row[6] is None else str(row[6]),
            )
            for row in rows
        ]

    # ----- loop ---------------------------------------------------------------------

    async def start(self) -> None:
        if self._task is not None:
            raise SchedulerError("scheduler already started")
        task = asyncio.get_running_loop().create_task(self._run(), name=_TASK_NAME)
        task.add_done_callback(_log_loop_exit)
        self._task = task

    async def stop(self) -> None:
        """Stop the loop. A handler in flight is cancelled and its event stays pending."""
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        await asyncio.wait({task})
        if not task.cancelled() and (exc := task.exception()) is not None:
            raise exc

    def close(self) -> None:
        """Close the database connection. Call ``stop()`` first."""
        if self._task is not None:
            raise SchedulerError("stop() the scheduler before close()")
        with self._lock:
            self._conn.close()

    async def _run(self) -> None:
        while True:
            # Clear before reading the head: any schedule/cancel after this point sets the
            # event again, so the wait below returns at once and re-reads the queue.
            self._wakeup.clear()
            head = self._head()
            if head is None:
                await self._wait(None)
                continue
            now = self._clock()
            if head.ready_at <= now:
                await self._deliver(head)
            else:
                await self._wait(head.ready_at - now)

    async def _wait(self, delay: float | None) -> None:
        """Sleep ``delay`` seconds (forever if None), returning early when woken."""
        wake = asyncio.ensure_future(self._wakeup.wait())
        waiting: set[asyncio.Future[Any]] = {wake}
        if delay is not None:
            waiting.add(asyncio.ensure_future(self._sleep(delay)))
        try:
            await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for fut in waiting:
                fut.cancel()
            await asyncio.wait(waiting)

    async def _deliver(self, head: _Head) -> None:
        event = head.event
        self._inflight = event.event_id
        try:
            async with asyncio.timeout(self._handler_timeout):
                await self._handler(event)
        except Exception as exc:
            # A timeout surfaces as TimeoutError and counts as a failed attempt like any other.
            self._record_failure(head, type(exc).__name__)
        else:
            self._mark_delivered(event.event_id)
            logger.debug("system event delivered: event_id=%s kind=%s", event.event_id, event.kind)
        finally:
            self._inflight = None

    def _head(self) -> _Head | None:
        """Earliest runnable event: ``max(due_at, next_attempt_at)`` then insertion order.

        Ordering by the retry time (not the due time) is what keeps a backing-off event from
        delaying other events whose due time has already passed.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT event_id, kind, user_id, payload, due_at, attempts, next_attempt_at "
                "FROM scheduled_events WHERE status = 'pending' "
                "ORDER BY CASE WHEN next_attempt_at > due_at THEN next_attempt_at ELSE due_at END, "
                "due_at, seq LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        return _Head(
            event=_event(row[:5]),
            attempts=int(row[5]),
            ready_at=max(float(row[4]), float(row[6])),
        )

    def _record_failure(self, head: _Head, error: str) -> None:
        attempts = head.attempts + 1
        event = head.event
        if attempts >= self._max_attempts:
            self._mark_failed(event.event_id, attempts, error)
            # Log identifiers and the exception type only; never the payload.
            logger.error(
                "system event failed permanently after %d attempts: event_id=%s kind=%s error=%s",
                attempts,
                event.event_id,
                event.kind,
                error,
            )
            self._notify_dead(event, error)
            return
        exponent = min(attempts - 1, _MAX_BACKOFF_EXPONENT)
        delay = min(self._retry_max, self._retry_base * (2.0**exponent))
        with self._lock, self._transaction() as conn:
            now = self._clock()
            conn.execute(
                "UPDATE scheduled_events SET attempts = ?, next_attempt_at = ?, "
                "last_error = ?, updated_at = ? WHERE event_id = ? AND status = 'pending'",
                (attempts, now + delay, error, now, event.event_id),
            )
        logger.warning(
            "system event handler failed, retry scheduled: event_id=%s kind=%s attempts=%d/%d "
            "retry_in=%.3fs error=%s",
            event.event_id,
            event.kind,
            attempts,
            self._max_attempts,
            delay,
            error,
        )

    def _notify_dead(self, event: SystemEvent, error: str) -> None:
        """Call ``on_dead`` once. A broken callback must not stop the loop."""
        if self._on_dead is None:
            return
        try:
            result = self._on_dead(event, error)
            if asyncio.iscoroutine(result):
                task = asyncio.get_running_loop().create_task(result, name="cgbot-on-dead")
                task.add_done_callback(_log_loop_exit)
        except Exception as exc:  # noqa: BLE001 - a callback error must not kill the loop
            logger.error("scheduler on_dead callback failed: error=%s", type(exc).__name__)

    def _mark_failed(self, event_id: str, attempts: int, error: str) -> None:
        with self._lock, self._transaction() as conn:
            conn.execute(
                "UPDATE scheduled_events SET status = 'failed', attempts = ?, next_attempt_at = 0, "
                "last_error = ?, updated_at = ? WHERE event_id = ? AND status = 'pending'",
                (attempts, error, self._clock(), event_id),
            )

    def _mark_delivered(self, event_id: str) -> None:
        with self._lock, self._transaction() as conn:
            conn.execute(
                "UPDATE scheduled_events SET status = 'delivered', updated_at = ? "
                "WHERE event_id = ? AND status = 'pending'",
                (self._clock(), event_id),
            )
