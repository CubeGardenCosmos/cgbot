"""Coding manager (M5 R5.3): per-user Codex sessions, instruction queue, turn bookkeeping.

Implements ``contracts.CodingService`` against the sandbox / connector / notifier Protocols only.
Invariants:
- at most ``governor.limit()`` running turns per user; a thread never runs two turns at once;
- queued instructions for one thread run FIFO; no steer is ever used to bypass the queue;
- user state is keyed by the ``user_id`` passed in by the caller (event or bearer identity);
- a Codex session (sandbox lease + websocket) is held only while the user is busy.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict, deque
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass

from cgbot.coding.config import TimeBudgetConfig
from cgbot.coding.contracts import (
    CodexClient,
    CodexConnector,
    CodexEvent,
    CodingNotice,
    CodingNotifier,
    CodingService,
    NoticeKind,
    SandboxProvider,
    SubmitResult,
    ThreadInfo,
    ThreadStatus,
)
from cgbot.coding.governor import ConcurrencyGovernor
from cgbot.coding.queue_store import QueueStore
from cgbot.dispatch.timebase import SystemTimebase, Timebase

logger = logging.getLogger(__name__)

MAX_START_ATTEMPTS = 3
MEMO_LIMIT = 256
TEXT_LIMIT = 4000
RECONNECT_MAX_DELAY = 60.0
BUDGET_RETRY_SECONDS = 5.0

DEFAULT_DONE_TEXT = "（任务已完成，无文本输出）"
DEFAULT_STOPPED_TEXT = "任务已停止"
DEFAULT_FAILED_TEXT = "任务执行失败"
VANISHED_TEXT = "沙盒连接中断，运行中的任务状态丢失，已记为失败"
START_FAILED_TEXT = "排队指令启动失败，已丢弃"
QUEUE_STARTED_TEXT = "排队指令开始执行"
TIME_BUDGET_PREFIX = "[系统时间预算] "
TIME_BUDGET_CANCELLED_TEXT = "任务已超过非重要任务的硬上限及收尾宽限，系统已强制中断"

PROGRESS_TIMEOUT = 90.0
PROGRESS_EMPTY_TEXT = "（没有拿到进展文本）"
PROGRESS_PREFIX = (
    "这是一次旁路进度询问，不是新任务。请用简短的纯文本回答："
    "已完成什么、正在做什么、还剩什么、有无阻塞、大致还需多久。"
    "不要调用任何工具，不要修改任何文件。\n\n问题："
)

_TURN_END: dict[str, tuple[NoticeKind, str]] = {
    "completed": ("turn_completed", DEFAULT_DONE_TEXT),
    "interrupted": ("turn_interrupted", DEFAULT_STOPPED_TEXT),
}
_TURN_FAILED: tuple[NoticeKind, str] = ("turn_failed", DEFAULT_FAILED_TEXT)

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[object]]


class CodingError(Exception):
    """User-facing failure; the message is safe to show to the user (no secrets, no bodies)."""


@dataclass(eq=False, slots=True)
class _Job:
    seq: int
    thread_id: str
    prompt: str
    title: str | None
    announce: bool
    important: bool = False
    time_limit_minutes: float | None = None
    attempts: int = 0


@dataclass(slots=True)
class _Memo:
    title: str | None = None
    last_text: str | None = None
    updated_at: float = 0.0


@dataclass(eq=False, slots=True)
class _Budget:
    turn_id: str
    important: bool
    cap_seconds: float
    soft_seconds: float
    offset: float  # seconds spent before ``base`` (restart recovery)
    base: float  # timebase.now() when this budget's timer started
    cancelled: bool = False
    timer: asyncio.Task[None] | None = None


@dataclass(eq=False, slots=True)
class _Session:
    stack: AsyncExitStack
    client: CodexClient
    pump: asyncio.Task[None] | None = None
    reconcile: asyncio.Task[None] | None = None
    dead: bool = False


class _UserState:
    def __init__(self, user_id: str) -> None:
        self.user_id = user_id
        self.lock = asyncio.Lock()
        self.queue: deque[_Job] = deque()
        self.running: dict[str, str] = {}  # thread_id -> turn_id
        self.budgets: dict[str, _Budget] = {}
        self.unverified: set[str] = set()  # running from before a session loss / restart
        self.awaiting: set[str] = set()  # resumed; waiting for a thread_status verdict
        self.memos: OrderedDict[str, _Memo] = OrderedDict()
        self.outbox: deque[CodingNotice] = deque()
        self.session: _Session | None = None
        self.refs = 0  # read operations currently holding the session
        self.reconnect: asyncio.Task[None] | None = None
        self.side: dict[str, asyncio.Future[CodexEvent]] = {}  # ephemeral fork id -> waiter
        self.side_lock = asyncio.Lock()  # one progress check per user at a time


def _is_busy(user: _UserState) -> bool:
    return bool(user.running) or bool(user.queue) or user.refs > 0


def _is_active(params: dict[str, object]) -> bool:
    status = params.get("status")
    if isinstance(status, dict):
        return status.get("type") == "active"
    return status in ("active", "running")


class CodingManager:
    def __init__(
        self,
        sandbox: SandboxProvider,
        connector: CodexConnector,
        notifier: CodingNotifier,
        governor: ConcurrencyGovernor,
        store: QueueStore,
        clock: Clock = time.time,
        *,
        sleep: Sleep = asyncio.sleep,
        reconcile_timeout: float = 30.0,
        max_queue: int = 200,
        time_budget: TimeBudgetConfig | None = None,
        timebase: Timebase | None = None,
    ) -> None:
        if reconcile_timeout <= 0:
            raise ValueError("reconcile_timeout must be positive")
        if max_queue < 1:
            raise ValueError("max_queue must be >= 1")
        self._sandbox = sandbox
        self._connector = connector
        self._notifier = notifier
        self._governor = governor
        self._store = store
        self._clock = clock
        self._sleep = sleep
        self._reconcile_timeout = reconcile_timeout
        self._max_queue = max_queue
        self._time_budget = time_budget if time_budget is not None else TimeBudgetConfig()
        self._timebase = timebase if timebase is not None else SystemTimebase()
        self._users: dict[str, _UserState] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._recovery: asyncio.Task[None] | None = None
        self._started = False
        self._closed = False
        governor.add_listener(self._on_limit_change)

    # ------------------------------------------------------------------ public API

    async def start(self) -> None:
        """Restore pending instructions and running turns from the store (idempotent)."""
        if self._closed:
            raise CodingError("coding manager is closed")
        if self._started:
            return
        for job in self._store.pending():
            user = self._user(job.user_id)
            user.queue.append(
                _Job(
                    seq=job.seq,
                    thread_id=job.thread_id,
                    prompt=job.prompt,
                    title=job.title,
                    announce=True,
                    important=job.important,
                    time_limit_minutes=job.time_limit_minutes,
                )
            )
            if job.title is not None:
                self._memo(user, job.thread_id).title = job.title
        for turn in self._store.running_turns():
            user = self._user(turn.user_id)
            user.running[turn.thread_id] = turn.turn_id
            self._arm_budget(
                user,
                turn.thread_id,
                turn.turn_id,
                turn.important,
                turn.time_limit_minutes,
                offset=max(0.0, self._clock() - turn.started_at),
            )
            user.unverified.add(turn.thread_id)
        self._started = True
        for user in list(self._users.values()):
            if _is_busy(user):
                self._spawn_reconnect(user)

    async def close(self) -> None:
        """Stop background work and close sessions. Codex turns keep running (recovered later)."""
        if self._closed:
            return
        self._closed = True
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for user in list(self._users.values()):
            async with user.lock:
                await self._drop_locked(user)
        self._store.close()

    async def submit(
        self,
        user_id: str,
        prompt: str,
        *,
        thread_id: str | None = None,
        title: str | None = None,
        important: bool = False,
        time_limit_minutes: float | None = None,
    ) -> SubmitResult:
        if time_limit_minutes is not None and not important:
            time_limit_minutes = None
        if time_limit_minutes is not None and not (
            0 < time_limit_minutes <= self._time_budget.max_important_minutes
        ):
            raise CodingError(
                "time_limit_minutes must be between 0 and "
                f"{self._time_budget.max_important_minutes}"
            )
        self._check_ready()
        user = self._user(user_id)
        if not prompt.strip():
            raise CodingError("prompt is empty")
        async with self._locked(user):
            if len(user.queue) >= self._max_queue:
                raise CodingError("queue is full")
            session = await self._ensure_session_locked(user)
            if thread_id is None:
                info = await self._guard("thread start", session.client.thread_start(title=title))
                thread = info.thread_id
                memo = self._memo(user, thread)
                memo.title = title or info.title
            else:
                thread = thread_id
                info = await self._verify(session.client, thread)
                memo = self._memo(user, thread)
                memo.title = title or info.title or memo.title
            seq = self._store.enqueue(
                user.user_id,
                thread,
                prompt,
                title,
                self._clock(),
                important=important,
                time_limit_minutes=time_limit_minutes,
            )
            job = _Job(
                seq=seq,
                thread_id=thread,
                prompt=prompt,
                title=title,
                announce=False,
                important=important,
                time_limit_minutes=time_limit_minutes,
            )
            user.queue.append(job)
            await self._dispatch_locked(user)
            if job in user.queue:
                job.announce = True
                position = sum(1 for j in user.queue if j.thread_id == thread)
                return SubmitResult(thread_id=thread, state="queued", queue_position=position)
            return SubmitResult(thread_id=thread, state="started", queue_position=0)

    async def list_threads(self, user_id: str) -> list[ThreadStatus]:
        self._check_ready()
        user = self._user(user_id)
        async with self._client_for(user) as client:
            infos = await self._guard("thread list", client.thread_list())
        result: list[ThreadStatus] = []
        seen: set[str] = set()
        for info in infos:
            if info.archived:
                continue
            memo = self._memo(user, info.thread_id)
            memo.title = info.title or memo.title
            memo.updated_at = info.updated_at
            seen.add(info.thread_id)
            result.append(self._status(user, info.thread_id))
        for thread in self._pending_threads(user):
            if thread not in seen:
                result.append(self._status(user, thread))
        return result

    async def peek(self, user_id: str, thread_id: str) -> ThreadStatus:
        self._check_ready()
        user = self._user(user_id)
        async with self._client_for(user) as client:
            info = await self._verify(client, thread_id)
        memo = self._memo(user, thread_id)
        memo.title = info.title or memo.title
        memo.updated_at = info.updated_at
        return self._status(user, thread_id)

    async def read(
        self,
        user_id: str,
        thread_id: str,
        *,
        cursor: str | None = None,
        limit: int = 20,
    ) -> tuple[list[dict[str, object]], str | None]:
        self._check_ready()
        user = self._user(user_id)
        async with self._client_for(user) as client:
            await self._verify(client, thread_id)
            return await self._guard(
                "thread read",
                client.thread_read(thread_id, cursor=cursor, limit=limit),
            )

    async def steer(self, user_id: str, thread_id: str, text: str) -> None:
        self._check_ready()
        user = self._user(user_id)
        if not text.strip():
            raise CodingError("text is empty")
        async with self._locked(user):
            if thread_id not in user.running:
                raise CodingError("no running turn to steer")
            session = await self._ensure_session_locked(user)
            turn_id = user.running.get(thread_id)
            if turn_id is None:
                raise CodingError("no running turn to steer")
            await self._guard("turn steer", session.client.turn_steer(thread_id, turn_id, text))

    async def stop(self, user_id: str, thread_id: str, *, clear_queue: bool = False) -> int:
        self._check_ready()
        user = self._user(user_id)
        async with self._locked(user):
            turn_id = user.running.get(thread_id)
            jobs = [j for j in user.queue if j.thread_id == thread_id]
            session = await self._ensure_session_locked(user)
            if turn_id is None and not jobs:
                await self._verify(session.client, thread_id)
                return 0
            if turn_id is not None:
                await self._guard(
                    "turn interrupt",
                    session.client.turn_interrupt(thread_id, turn_id),
                )
            dropped = 0
            if clear_queue and jobs:
                for job in jobs:
                    user.queue.remove(job)
                self._store.delete_jobs([j.seq for j in jobs])
                dropped = len(jobs)
                self._notify(user, "queue_dropped", thread_id, f"已清空 {dropped} 条排队指令")
            return dropped

    async def compact(self, user_id: str, thread_id: str) -> None:
        self._check_ready()
        user = self._user(user_id)
        async with self._client_for(user) as client:
            await self._verify(client, thread_id)
            await self._guard("thread compact", client.thread_compact(thread_id))

    async def rename(self, user_id: str, thread_id: str, title: str) -> None:
        self._check_ready()
        user = self._user(user_id)
        async with self._client_for(user) as client:
            await self._verify(client, thread_id)
            await self._guard("thread rename", client.thread_rename(thread_id, title))
        self._memo(user, thread_id).title = title

    async def archive(self, user_id: str, thread_id: str) -> None:
        self._check_ready()
        user = self._user(user_id)
        async with self._locked(user):
            self._require_idle_thread(user, thread_id)
            session = await self._ensure_session_locked(user)
            await self._verify(session.client, thread_id)
            await self._guard("thread archive", session.client.thread_archive(thread_id))
            user.memos.pop(thread_id, None)

    async def delete(self, user_id: str, thread_id: str) -> None:
        self._check_ready()
        user = self._user(user_id)
        async with self._locked(user):
            self._require_idle_thread(user, thread_id)
            session = await self._ensure_session_locked(user)
            await self._verify(session.client, thread_id)
            await self._guard("thread delete", session.client.thread_delete(thread_id))
            user.memos.pop(thread_id, None)

    async def fork(self, user_id: str, thread_id: str) -> SubmitResult:
        """Fork a thread; the fork is created with state "started" and position 0 (no turn)."""
        self._check_ready()
        user = self._user(user_id)
        async with self._client_for(user) as client:
            await self._verify(client, thread_id)
            info = await self._guard("thread fork", client.thread_fork(thread_id))
        self._memo(user, info.thread_id).title = info.title
        return SubmitResult(thread_id=info.thread_id, state="started", queue_position=0)

    async def ask_progress(
        self, user_id: str, thread_id: str, question: str, *, wait_seconds: float = PROGRESS_TIMEOUT
    ) -> str:
        """Ask Codex how a thread is going via an ephemeral fork; the running turn is untouched.

        Returns Codex's own answer text. The fork is never persisted or listed; before this
        returns it is unsubscribed (and its side turn interrupted if still running).
        """
        self._check_ready()
        user = self._user(user_id)
        if self._governor.degraded:
            raise CodingError("Codex is rate-limited right now; try again shortly")
        if user.side_lock.locked():
            raise CodingError("another progress check is already running")
        async with user.side_lock:
            async with self._client_for(user) as client:
                await self._verify(client, thread_id)
                fork = await self._guard(
                    "progress fork", client.thread_fork(thread_id, ephemeral=True)
                )
                fork_id = fork.thread_id
                waiter: asyncio.Future[CodexEvent] = asyncio.get_running_loop().create_future()
                user.side[fork_id] = waiter
                turn_id: str | None = None
                try:
                    turn_id = await self._guard(
                        "progress turn", client.turn_start(fork_id, PROGRESS_PREFIX + question)
                    )
                    await asyncio.wait({waiter}, timeout=wait_seconds)
                    if not waiter.done():
                        raise CodingError("progress check timed out")
                    event = waiter.result()
                finally:
                    user.side.pop(fork_id, None)
                    await self._discard_side(client, fork_id, None if waiter.done() else turn_id)
        if event.status != "completed":
            raise CodingError("progress check did not complete")
        return (event.text or "").strip()[:TEXT_LIMIT] or PROGRESS_EMPTY_TEXT

    # ------------------------------------------------------------------ helpers: state

    def _check_ready(self) -> None:
        if self._closed:
            raise CodingError("coding manager is closed")
        if not self._started:
            raise CodingError("coding manager not started")

    def _user(self, user_id: str) -> _UserState:
        if not user_id:
            raise ValueError("user_id is required")
        state = self._users.get(user_id)
        if state is None:
            state = _UserState(user_id)
            self._users[user_id] = state
        return state

    def _memo(self, user: _UserState, thread_id: str) -> _Memo:
        memo = user.memos.get(thread_id)
        if memo is None:
            memo = _Memo()
            user.memos[thread_id] = memo
            while len(user.memos) > MEMO_LIMIT:
                user.memos.popitem(last=False)
        else:
            user.memos.move_to_end(thread_id)
        return memo

    def _pending_threads(self, user: _UserState) -> list[str]:
        threads = list(user.running)
        for job in user.queue:
            if job.thread_id not in threads:
                threads.append(job.thread_id)
        return threads

    def _status(self, user: _UserState, thread_id: str) -> ThreadStatus:
        memo = self._memo(user, thread_id)
        queued = sum(1 for j in user.queue if j.thread_id == thread_id)
        return ThreadStatus(
            thread_id=thread_id,
            title=memo.title,
            running=thread_id in user.running,
            queued=queued,
            last_text=memo.last_text,
            updated_at=memo.updated_at,
        )

    def _require_idle_thread(self, user: _UserState, thread_id: str) -> None:
        if thread_id in user.running or any(j.thread_id == thread_id for j in user.queue):
            raise CodingError("thread is busy")

    def _notify(self, user: _UserState, kind: NoticeKind, thread_id: str | None, text: str) -> None:
        memo = user.memos.get(thread_id) if thread_id is not None else None
        user.outbox.append(
            CodingNotice(
                kind=kind,
                user_id=user.user_id,
                thread_id=thread_id,
                title=memo.title if memo is not None else None,
                text=text,
            )
        )

    async def _flush(self, user: _UserState) -> None:
        while user.outbox:
            notice = user.outbox.popleft()
            try:
                await self._notifier.notify(notice)
            except Exception as exc:
                logger.warning("coding notifier failed (%s)", type(exc).__name__)

    @asynccontextmanager
    async def _locked(self, user: _UserState) -> AsyncIterator[None]:
        """Hold the user lock; release the session when idle; send notices after unlocking."""
        try:
            async with user.lock:
                try:
                    yield
                finally:
                    if user.session is not None and not _is_busy(user):
                        await self._drop_locked(user)
        finally:
            await self._flush(user)

    @asynccontextmanager
    async def _client_for(self, user: _UserState) -> AsyncIterator[CodexClient]:
        """Hold the session for one read operation (refcounted so it is not released)."""
        async with self._locked(user):
            session = await self._ensure_session_locked(user)
            user.refs += 1
        try:
            yield session.client
        finally:
            async with self._locked(user):
                user.refs -= 1

    async def _guard[T](self, op: str, awaitable: Awaitable[T]) -> T:
        try:
            return await awaitable
        except CodingError:
            raise
        except Exception as exc:
            logger.warning("codex %s failed (%s)", op, type(exc).__name__)
            raise CodingError(f"{op} failed") from exc

    async def _verify(self, client: CodexClient, thread_id: str) -> ThreadInfo:
        """Confirm the thread exists in this user's sandbox (resume is the ownership check)."""
        try:
            return await client.thread_resume(thread_id)
        except Exception as exc:
            logger.info("thread verify failed (%s)", type(exc).__name__)
            raise CodingError("thread not found") from exc

    async def _discard_side(self, client: CodexClient, fork_id: str, turn_id: str | None) -> None:
        """Best-effort cleanup of an ephemeral side fork (codex refuses archive/delete for it)."""
        if turn_id is not None:
            try:
                await client.turn_interrupt(fork_id, turn_id)
            except Exception as exc:
                logger.info("progress turn interrupt failed (%s)", type(exc).__name__)
        try:
            await client.thread_unsubscribe(fork_id)
        except Exception as exc:
            logger.info("progress fork unsubscribe failed (%s)", type(exc).__name__)

    # ------------------------------------------------------------------ helpers: sessions

    async def _ensure_session_locked(self, user: _UserState) -> _Session:
        session = user.session
        if session is not None and session.dead:
            await self._drop_locked(user)
            session = None
        if session is None:
            session = await self._open_locked(user)
        return session

    async def _open_locked(self, user: _UserState) -> _Session:
        stack = AsyncExitStack()
        try:
            endpoint = await self._guard(
                "sandbox lease",
                stack.enter_async_context(self._sandbox.lease(user.user_id)),
            )
            client = await self._guard("codex connect", self._connector(endpoint))
            stack.push_async_callback(client.close)
        except BaseException:
            await stack.aclose()
            raise
        session = _Session(stack=stack, client=client)
        user.session = session
        session.pump = self._spawn(self._pump(user, session))
        targets = sorted(t for t in user.unverified if t in user.running)
        user.unverified.clear()
        if targets:
            session.reconcile = self._spawn(self._reconcile(user, session, targets))
        return session

    async def _drop_locked(self, user: _UserState) -> None:
        session = user.session
        if session is None:
            return
        user.session = None
        session.dead = True
        current = asyncio.current_task()
        for task in (session.pump, session.reconcile):
            if task is not None and task is not current and not task.done():
                task.cancel()
        user.unverified.update(user.running)
        user.awaiting.clear()
        try:
            await session.stack.aclose()
        except Exception as exc:
            logger.warning("closing codex session failed (%s)", type(exc).__name__)

    def _spawn(self, coro: Coroutine[object, object, None]) -> asyncio.Task[None]:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _spawn_reconnect(self, user: _UserState) -> None:
        if self._closed:
            return
        if user.reconnect is not None and not user.reconnect.done():
            return
        user.reconnect = self._spawn(self._reconnect(user))

    async def _reconnect(self, user: _UserState) -> None:
        attempt = 0
        while not self._closed:
            async with self._locked(user):
                session = user.session
                if session is not None and session.dead:
                    await self._drop_locked(user)
                    session = None
                if session is not None or not _is_busy(user):
                    return
            if attempt:
                await self._sleep(min(RECONNECT_MAX_DELAY, float(2 ** (attempt - 1))))
            attempt += 1
            async with self._locked(user):
                if user.session is not None or not _is_busy(user):
                    continue
                try:
                    await self._open_locked(user)
                except CodingError:
                    logger.warning("codex reconnect failed; retrying")
                    continue
                attempt = 0
                await self._dispatch_locked(user)

    async def _pump(self, user: _UserState, session: _Session) -> None:
        try:
            async for event in session.client.events():
                async with self._locked(user):
                    if user.session is not session:
                        return
                    try:
                        await self._handle_event_locked(user, event)
                    except Exception as exc:
                        logger.warning("handling codex event failed (%s)", type(exc).__name__)
                if user.session is not session:
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("codex event stream failed (%s)", type(exc).__name__)
        async with self._locked(user):
            if user.session is session and not session.dead:
                session.dead = True
                if self._closed or not _is_busy(user):
                    await self._drop_locked(user)
                else:
                    self._spawn_reconnect(user)

    async def _reconcile(self, user: _UserState, session: _Session, threads: list[str]) -> None:
        """Resume threads that were running before the session was (re)opened."""
        for thread in threads:
            async with self._locked(user):
                if user.session is not session or thread not in user.running:
                    continue
                try:
                    await session.client.thread_resume(thread)
                except Exception as exc:
                    logger.warning("reconcile resume failed (%s)", type(exc).__name__)
                    self._vanish_locked(user, thread)
                    await self._dispatch_locked(user)
                    continue
                user.awaiting.add(thread)
        if not user.awaiting:
            return
        await self._sleep(self._reconcile_timeout)
        async with self._locked(user):
            if user.session is not session:
                return
            for thread in sorted(user.awaiting):
                self._vanish_locked(user, thread)
            user.awaiting.clear()
            await self._dispatch_locked(user)

    def _vanish_locked(self, user: _UserState, thread_id: str) -> None:
        """A turn that cannot be confirmed as running is recorded as failed (exactly once)."""
        if user.running.pop(thread_id, None) is None:
            return
        self._stop_budget(user, thread_id)
        user.awaiting.discard(thread_id)
        user.unverified.discard(thread_id)
        self._store.finish_turn(user.user_id, thread_id)
        self._notify(user, "turn_failed", thread_id, VANISHED_TEXT)

    # ------------------------------------------------------------------ helpers: events / dispatch

    async def _handle_event_locked(self, user: _UserState, event: CodexEvent) -> None:
        if event.kind == "turn_completed":
            self._settle_side_locked(user, event)
            self._on_turn_completed_locked(user, event)
        elif event.kind == "thread_status":
            self._on_thread_status_locked(user, event)
        elif event.kind == "error":
            logger.warning("codex reported an error event")
        await self._dispatch_locked(user)

    def _settle_side_locked(self, user: _UserState, event: CodexEvent) -> None:
        waiter = user.side.get(event.thread_id) if event.thread_id is not None else None
        if waiter is not None and not waiter.done():
            waiter.set_result(event)

    def _on_turn_completed_locked(self, user: _UserState, event: CodexEvent) -> None:
        thread = event.thread_id
        turn = event.turn_id
        if thread is None or turn is None or user.running.get(thread) != turn:
            return
        del user.running[thread]
        budget = user.budgets.get(thread)
        cancelled = budget is not None and budget.cancelled
        self._stop_budget(user, thread)
        user.unverified.discard(thread)
        user.awaiting.discard(thread)
        self._store.finish_turn(user.user_id, thread)
        kind, default_text = _TURN_END.get(event.status or "", _TURN_FAILED)
        text = (event.text or "").strip() or default_text
        memo = self._memo(user, thread)
        if cancelled:
            partial = (event.text or "").strip()
            if not partial:
                partial = (
                    f"本次未返回文本结果；上一次记录的结果（非本次）：{memo.last_text}"
                    if memo.last_text
                    else "尚无文本结果，已有输出仍保留在线程中。"
                )
            text = f"{TIME_BUDGET_CANCELLED_TEXT}。\n已保留部分结果：{partial}"
        memo.last_text = text[:TEXT_LIMIT]
        memo.updated_at = self._clock()
        self._notify(user, kind, thread, text)

    def _on_thread_status_locked(self, user: _UserState, event: CodexEvent) -> None:
        thread = event.thread_id
        if thread is None or thread not in user.awaiting:
            return
        user.awaiting.discard(thread)
        if not _is_active(event.params):
            self._vanish_locked(user, thread)

    # ------------------------------------------------------------------ helpers: time budget

    def _budget_cap(self, important: bool, time_limit_minutes: float | None) -> float:
        if important and time_limit_minutes is not None:
            return time_limit_minutes * 60.0
        return self._time_budget.cap_seconds

    def _arm_budget(
        self,
        user: _UserState,
        thread_id: str,
        turn_id: str,
        important: bool,
        time_limit_minutes: float | None,
        *,
        offset: float,
    ) -> None:
        """Start the single timer of an executing turn; ``offset`` is time spent before now."""
        self._stop_budget(user, thread_id)
        cap = self._budget_cap(important, time_limit_minutes)
        budget = _Budget(
            turn_id=turn_id,
            important=important,
            cap_seconds=cap,
            soft_seconds=min(self._time_budget.soft_target_seconds, cap),
            offset=offset,
            base=self._timebase.now(),
        )
        user.budgets[thread_id] = budget
        budget.timer = self._spawn(self._budget_loop(user.user_id, thread_id, budget))

    def _budget_elapsed(self, budget: _Budget) -> float:
        return budget.offset + self._timebase.now() - budget.base

    @staticmethod
    def _budget_current(user: _UserState, thread_id: str, budget: _Budget) -> bool:
        return (
            user.budgets.get(thread_id) is budget and user.running.get(thread_id) == budget.turn_id
        )

    @staticmethod
    def _stop_budget(user: _UserState, thread_id: str) -> None:
        budget = user.budgets.pop(thread_id, None)
        if budget is None or budget.timer is None:
            return
        if budget.timer is not asyncio.current_task():
            budget.timer.cancel()
        budget.timer = None

    def _periodic_interval(self, budget: _Budget, elapsed: float) -> float:
        """Periodic interval in effect at ``elapsed``; sampled when a notice fires."""
        config = self._time_budget
        if elapsed >= budget.cap_seconds - config.approach_window_seconds:
            return config.approach_interval_seconds
        return config.notice_interval_seconds

    def _next_budget_point(
        self, budget: _Budget, done: set[str], next_periodic: float
    ) -> tuple[float, str]:
        """Next due point: a pending milestone (possibly already due) or the next periodic tick."""
        config = self._time_budget
        window_start = budget.cap_seconds - config.approach_window_seconds
        points: list[tuple[float, int, str]] = []
        if "soft" not in done:
            points.append((budget.soft_seconds, 0, "soft"))
        if "approach" not in done and window_start > 0:
            points.append((window_start, 1, "approach"))
        if "cap" not in done:
            points.append((budget.cap_seconds, 2, "cap"))
        if not budget.important and "hard" not in done:
            points.append((budget.cap_seconds + config.grace_seconds, 3, "hard"))
        points.append((next_periodic, 4, "periodic"))
        due, _, kind = min(points)
        return due, kind

    def _budget_notice(self, budget: _Budget, elapsed: float, kind: str) -> str:
        cap = budget.cap_seconds
        if kind == "soft":
            body = (
                f"已运行 {elapsed:.0f} 秒，达到 {budget.soft_seconds:.0f} 秒软目标；"
                f"距离 {cap:.0f} 秒上限还剩 {max(0.0, cap - elapsed):.0f} 秒。"
                "请立即产出可用结果并结束，不要继续打磨。"
            )
        elif elapsed < cap:
            body = (
                f"已运行 {elapsed:.0f} 秒，距离 {cap:.0f} 秒上限还剩 "
                f"{cap - elapsed:.0f} 秒。请收尾并返回已有结果。"
            )
        elif budget.important:
            body = (
                f"已运行 {elapsed:.0f} 秒，超过 {cap:.0f} 秒上限 {elapsed - cap:.0f} 秒。"
                "请立即收尾并返回已有结果，不要继续扩展工作。"
            )
        else:
            grace_left = max(0.0, cap + self._time_budget.grace_seconds - elapsed)
            body = (
                f"已达到 {cap:.0f} 秒硬上限，收尾宽限剩余 {grace_left:.0f} 秒。"
                "立即返回已有结果，不得开启新检查。"
            )
        return TIME_BUDGET_PREFIX + body

    async def _budget_loop(self, user_id: str, thread_id: str, budget: _Budget) -> None:
        """Drive one turn's budget: notices generated by code; hard interrupt for ordinary tasks."""
        done: set[str] = set()
        if budget.soft_seconds >= budget.cap_seconds:
            done.add("soft")
        next_periodic = self._periodic_interval(budget, 0.0)
        while True:
            elapsed = self._budget_elapsed(budget)
            due, kind = self._next_budget_point(budget, done, next_periodic)
            if due > elapsed:
                await self._timebase.sleep(due - elapsed)
                continue
            text = None if kind == "hard" else self._budget_notice(budget, elapsed, kind)
            outcome = await self._budget_call(user_id, thread_id, budget, text=text)
            if outcome == "stop":
                return
            if outcome == "retry":
                await self._timebase.sleep(BUDGET_RETRY_SECONDS)
                continue
            if kind != "periodic":
                done.add(kind)
            fired_at = self._budget_elapsed(budget)
            next_periodic = fired_at + self._periodic_interval(budget, fired_at)

    async def _budget_call(
        self, user_id: str, thread_id: str, budget: _Budget, *, text: str | None
    ) -> str:
        """Steer with ``text``, or hard-interrupt when ``text`` is None.

        Returns ``"done"``, ``"stop"`` (the timer must end) or ``"retry"`` (try again later).
        """
        user = self._user(user_id)
        async with self._locked(user):
            if not self._budget_current(user, thread_id, budget):
                return "stop"
            try:
                session = await self._ensure_session_locked(user)
            except CodingError:
                logger.warning("time budget: codex session unavailable; retrying")
                return "retry"
            if text is None:
                budget.cancelled = True
        try:
            if text is None:
                await session.client.turn_interrupt(thread_id=thread_id, turn_id=budget.turn_id)
                return "stop"
            await session.client.turn_steer(thread_id=thread_id, turn_id=budget.turn_id, text=text)
        except Exception as exc:
            budget.cancelled = False
            logger.warning("time budget action failed (%s)", type(exc).__name__)
            return "retry"
        async with self._locked(user):
            if self._budget_current(user, thread_id, budget):
                self._notify(user, "time_budget", thread_id, text)
        return "done"

    async def _dispatch_locked(self, user: _UserState) -> None:
        """Start queued instructions while a slot is free and their thread is idle."""
        while True:
            session = user.session
            if session is None or session.dead:
                return
            if len(user.running) >= self._governor.limit():
                return
            job = next((j for j in user.queue if j.thread_id not in user.running), None)
            if job is None:
                return
            try:
                turn_id = await session.client.turn_start(job.thread_id, job.prompt)
            except Exception as exc:
                job.attempts += 1
                logger.warning(
                    "codex turn start failed (%s), attempt %d", type(exc).__name__, job.attempts
                )
                if job.attempts >= MAX_START_ATTEMPTS:
                    user.queue.remove(job)
                    self._store.delete_jobs([job.seq])
                    self._notify(user, "turn_failed", job.thread_id, START_FAILED_TEXT)
                    continue
                await self._drop_locked(user)
                if _is_busy(user):
                    self._spawn_reconnect(user)
                return
            user.queue.remove(job)
            user.running[job.thread_id] = turn_id
            started_at = self._clock()
            self._store.start_job(
                job.seq,
                user.user_id,
                job.thread_id,
                turn_id,
                started_at,
                important=job.important,
                time_limit_minutes=job.time_limit_minutes,
            )
            self._arm_budget(
                user, job.thread_id, turn_id, job.important, job.time_limit_minutes, offset=0.0
            )
            if job.announce:
                self._notify(user, "queue_started", job.thread_id, QUEUE_STARTED_TEXT)

    async def _dispatch_all(self) -> None:
        for user in list(self._users.values()):
            if self._closed:
                return
            if not user.queue:
                continue
            async with self._locked(user):
                await self._dispatch_locked(user)

    def _on_limit_change(self, limit: int) -> None:
        if self._closed or not self._started:
            return
        self._spawn(self._dispatch_all())
        degraded = limit < self._governor.per_user
        if degraded and (self._recovery is None or self._recovery.done()):
            self._recovery = self._spawn(self._await_recovery())

    async def _await_recovery(self) -> None:
        """Sleep until the governor's recovery point, then refresh it so the limit is restored."""
        while not self._closed:
            due = self._governor.recovers_at()
            if due is None:
                return
            await self._sleep(max(0.0, due - self._clock()))
            self._governor.limit()


def _as_service(manager: CodingManager) -> CodingService:
    """Static check that the manager satisfies the contract."""
    return manager
