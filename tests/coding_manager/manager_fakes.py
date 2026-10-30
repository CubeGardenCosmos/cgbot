"""Deterministic fakes for the coding manager tests (no network, no real clock)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path

from cgbot.coding.config import ConcurrencyConfig
from cgbot.coding.contracts import CodexEvent, CodingNotice, SandboxEndpoint, ThreadInfo
from cgbot.coding.governor import ConcurrencyGovernor
from cgbot.coding.manager import CodingManager
from cgbot.coding.queue_store import QueueStore

CONFIG = ConcurrencyConfig(
    per_user=4,
    degraded=1,
    degrade_failures=3,
    degrade_window_seconds=60.0,
    recover_after_seconds=3600.0,
)


class FakeClock:
    """Manual clock; ``sleep`` parks until ``advance`` passes its deadline."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start
        self._sleepers: list[tuple[float, asyncio.Future[None]]] = []

    def time(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._sleepers.append((self.now + delay, future))
        await future

    def advance(self, seconds: float) -> None:
        self.now += seconds
        due = [future for at, future in self._sleepers if at <= self.now]
        self._sleepers = [(at, f) for at, f in self._sleepers if at > self.now]
        for future in due:
            if not future.done():
                future.set_result(None)

    @property
    def sleepers(self) -> int:
        return len(self._sleepers)


async def until(predicate: Callable[[], bool], rounds: int = 1000) -> None:
    for _ in range(rounds):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition not reached")


async def settle(rounds: int = 50) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)


@dataclass
class UserSandbox:
    """State of one user's Codex app-server; it outlives manager sessions (like a container)."""

    threads: dict[str, ThreadInfo] = field(default_factory=dict)
    active: dict[str, str] = field(default_factory=dict)  # thread_id -> running turn_id
    clients: list[FakeCodexClient] = field(default_factory=list)
    started: list[tuple[str, str, str]] = field(default_factory=list)  # (thread, prompt, turn)
    steers: list[tuple[str, str, str]] = field(default_factory=list)  # (thread, turn, text)
    interrupts: list[tuple[str, str]] = field(default_factory=list)  # (thread, turn)
    compacted: list[str] = field(default_factory=list)
    renamed: list[tuple[str, str]] = field(default_factory=list)
    resume_status: str | None = None  # emit thread_status on resume when set
    resume_fails: bool = False
    turn_start_failures: int = 0


class FakeCodexServer:
    def __init__(self) -> None:
        self._users: dict[str, UserSandbox] = {}
        self._seq = 0

    def user(self, user_id: str) -> UserSandbox:
        return self._users.setdefault(user_id, UserSandbox())

    def new_thread_id(self) -> str:
        self._seq += 1
        return f"thread-{self._seq}"

    def new_turn_id(self) -> str:
        self._seq += 1
        return f"turn-{self._seq}"

    def connect(self, user_id: str) -> FakeCodexClient:
        client = FakeCodexClient(self, user_id)
        self.user(user_id).clients.append(client)
        return client

    def disconnect(self, user_id: str, client: FakeCodexClient) -> None:
        clients = self.user(user_id).clients
        if client in clients:
            clients.remove(client)

    def emit(self, user_id: str, event: CodexEvent) -> None:
        for client in self.user(user_id).clients:
            client.push(event)

    def complete(
        self,
        user_id: str,
        thread_id: str,
        *,
        status: str = "completed",
        text: str | None = "done",
    ) -> str:
        turn = self.user(user_id).active.pop(thread_id)
        self.emit(
            user_id,
            CodexEvent(
                kind="turn_completed",
                thread_id=thread_id,
                turn_id=turn,
                status=status,
                text=text,
            ),
        )
        return turn

    def forget_turn(self, user_id: str, thread_id: str) -> None:
        """Simulate the server having lost a turn without emitting anything."""
        self.user(user_id).active.pop(thread_id, None)

    def drop(self, user_id: str) -> None:
        """Simulate the websocket dropping: every live stream ends."""
        for client in list(self.user(user_id).clients):
            client.end()


class FakeCodexClient:
    def __init__(self, server: FakeCodexServer, user_id: str) -> None:
        self._server = server
        self._user_id = user_id
        self._queue: asyncio.Queue[CodexEvent | None] = asyncio.Queue()
        self.closed = False

    @property
    def _sb(self) -> UserSandbox:
        return self._server.user(self._user_id)

    def push(self, event: CodexEvent) -> None:
        self._queue.put_nowait(event)

    def end(self) -> None:
        self._queue.put_nowait(None)

    async def events(self) -> AsyncIterator[CodexEvent]:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item

    async def close(self) -> None:
        self.closed = True
        self._server.disconnect(self._user_id, self)
        self.end()

    def _info(self, thread_id: str) -> ThreadInfo:
        info = self._sb.threads.get(thread_id)
        if info is None:
            raise KeyError(thread_id)
        return info

    async def thread_start(self, *, title: str | None = None) -> ThreadInfo:
        info = ThreadInfo(thread_id=self._server.new_thread_id(), title=title, updated_at=0.0)
        self._sb.threads[info.thread_id] = info
        return info

    async def thread_resume(self, thread_id: str) -> ThreadInfo:
        info = self._info(thread_id)
        if self._sb.resume_fails:
            raise RuntimeError("resume failed")
        if self._sb.resume_status is not None:
            self.push(
                CodexEvent(
                    kind="thread_status",
                    thread_id=thread_id,
                    turn_id=None,
                    params={"status": {"type": self._sb.resume_status}},
                )
            )
        return info

    async def thread_list(self, *, include_archived: bool = False) -> list[ThreadInfo]:
        return [t for t in self._sb.threads.values() if include_archived or not t.archived]

    async def thread_read(
        self,
        thread_id: str,
        *,
        cursor: str | None = None,
        limit: int = 20,
    ) -> tuple[list[dict[str, object]], str | None]:
        self._info(thread_id)
        return [{"type": "text", "text": "hello"}], None

    async def thread_rename(self, thread_id: str, title: str) -> None:
        self._sb.threads[thread_id] = replace(self._info(thread_id), title=title)
        self._sb.renamed.append((thread_id, title))

    async def thread_archive(self, thread_id: str) -> None:
        self._sb.threads[thread_id] = replace(self._info(thread_id), archived=True)

    async def thread_delete(self, thread_id: str) -> None:
        self._info(thread_id)
        del self._sb.threads[thread_id]

    async def thread_fork(self, thread_id: str) -> ThreadInfo:
        parent = self._info(thread_id)
        forked = ThreadInfo(
            thread_id=self._server.new_thread_id(), title=parent.title, updated_at=0.0
        )
        self._sb.threads[forked.thread_id] = forked
        return forked

    async def thread_compact(self, thread_id: str) -> None:
        self._info(thread_id)
        self._sb.compacted.append(thread_id)

    async def turn_start(self, thread_id: str, text: str) -> str:
        sb = self._sb
        self._info(thread_id)
        if sb.turn_start_failures > 0:
            sb.turn_start_failures -= 1
            raise RuntimeError("turn rejected")
        if thread_id in sb.active:
            raise RuntimeError("thread busy")
        turn = self._server.new_turn_id()
        sb.active[thread_id] = turn
        sb.started.append((thread_id, text, turn))
        return turn

    async def turn_steer(self, thread_id: str, turn_id: str, text: str) -> None:
        if self._sb.active.get(thread_id) != turn_id:
            raise RuntimeError("expected turn mismatch")
        self._sb.steers.append((thread_id, turn_id, text))

    async def turn_interrupt(self, thread_id: str, turn_id: str) -> None:
        if self._sb.active.get(thread_id) != turn_id:
            raise RuntimeError("no such turn")
        self._sb.interrupts.append((thread_id, turn_id))
        self._server.complete(self._user_id, thread_id, status="interrupted", text=None)


class FakeConnector:
    def __init__(self, server: FakeCodexServer) -> None:
        self._server = server
        self.connects = 0
        self.fail_next = 0

    async def __call__(self, endpoint: SandboxEndpoint) -> FakeCodexClient:
        if self.fail_next > 0:
            self.fail_next -= 1
            raise RuntimeError("connection refused")
        self.connects += 1
        return self._server.connect(endpoint.user_id)


class FakeSandbox:
    def __init__(self) -> None:
        self.open_count = 0
        self.leases = 0
        self.stopped: list[str] = []
        self.closed = False
        self.fail = False

    def lease(self, user_id: str) -> AbstractAsyncContextManager[SandboxEndpoint]:
        return self._lease(user_id)

    @asynccontextmanager
    async def _lease(self, user_id: str) -> AsyncIterator[SandboxEndpoint]:
        if self.fail:
            raise RuntimeError("sandbox unavailable")
        self.leases += 1
        self.open_count += 1
        try:
            yield SandboxEndpoint(
                user_id=user_id,
                container=f"c-{user_id}",
                url="ws://c-1:8080",
                ws_token="ws-secret",
            )
        finally:
            self.open_count -= 1

    async def stop(self, user_id: str) -> None:
        self.stopped.append(user_id)

    async def close(self) -> None:
        self.closed = True


class FakeNotifier:
    def __init__(self) -> None:
        self.notices: list[CodingNotice] = []
        self.fail = False

    async def notify(self, notice: CodingNotice) -> None:
        if self.fail:
            raise RuntimeError("send failed")
        self.notices.append(notice)

    def of(self, kind: str) -> list[CodingNotice]:
        return [n for n in self.notices if n.kind == kind]

    def count(self, kind: str) -> int:
        return len(self.of(kind))


@dataclass
class Rig:
    manager: CodingManager
    store: QueueStore
    clock: FakeClock
    server: FakeCodexServer
    sandbox: FakeSandbox
    connector: FakeConnector
    notifier: FakeNotifier
    governor: ConcurrencyGovernor


def build(
    path: Path,
    *,
    clock: FakeClock | None = None,
    server: FakeCodexServer | None = None,
    config: ConcurrencyConfig = CONFIG,
    reconcile_timeout: float = 30.0,
    max_queue: int = 200,
) -> Rig:
    """Assemble a manager over fakes. Pass the same clock/server/path to simulate a restart."""
    if clock is None:
        clock = FakeClock()
    if server is None:
        server = FakeCodexServer()
    sandbox = FakeSandbox()
    connector = FakeConnector(server)
    notifier = FakeNotifier()
    governor = ConcurrencyGovernor(config, clock.time)
    store = QueueStore(path)
    manager = CodingManager(
        sandbox,
        connector,
        notifier,
        governor,
        store,
        clock.time,
        sleep=clock.sleep,
        reconcile_timeout=reconcile_timeout,
        max_queue=max_queue,
    )
    return Rig(
        manager=manager,
        store=store,
        clock=clock,
        server=server,
        sandbox=sandbox,
        connector=connector,
        notifier=notifier,
        governor=governor,
    )
