"""Cross-slice contracts for M5 (Coding Agent). Owned by lead; extend only additively.

Topology (single bot process + one container per user)::

    NapCat ──ws──► cgbot (one process, one aiohttp server on :8000)
                    ├─ gateway   /v1/*       sandbox → real upstream (Responses, thin pass-through)
                    ├─ bridge    /bridge/*   sandbox → bot (report_progress, files, generate_image)
                    ├─ manager   queues, concurrency, thread tools
                    └─ sandbox lifecycle via docker-socket-proxy (HTTP)
    sandbox container (per user): `codex app-server --listen ws://0.0.0.0:<port>` (YOLO)
        MCP servers inside: cgbot bridge (stdio), Playwright, LSP (Python / Node / TypeScript)

Identity: sandbox bearer tokens are ``SandboxAuth.token_for(user_id)``; the gateway and the bridge
resolve the token back to a user id. Tools and bridge calls never take a user id from the model.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

# ---------------------------------------------------------------- gateway -> manager

# Called by the gateway for every upstream response status (0 = connection failure / timeout).
UpstreamStatusHook = Callable[[int], None]


class SandboxAuth(Protocol):
    """HMAC-derived bearer tokens: stateless, so a bot restart keeps live sandboxes valid."""

    def token_for(self, user_id: str) -> str: ...

    def resolve(self, token: str) -> str | None: ...


# ---------------------------------------------------------------- sandbox lifecycle (B)


@dataclass(frozen=True, slots=True)
class SandboxEndpoint:
    user_id: str
    container: str
    url: str  # ws://<container>:<port>
    ws_token: str  # capability token for the app-server websocket (never logged)


class SandboxProvider(Protocol):
    def lease(self, user_id: str) -> AbstractAsyncContextManager[SandboxEndpoint]:
        """Create/start the user's container if needed, wait until app-server is ready, and
        hold it against the idle reaper until the context exits."""
        ...

    async def stop(self, user_id: str) -> None: ...

    async def close(self) -> None: ...


# ---------------------------------------------------------------- Codex app-server client (C)


@dataclass(frozen=True, slots=True)
class ThreadInfo:
    thread_id: str
    title: str | None
    updated_at: float
    archived: bool = False


@dataclass(frozen=True, slots=True)
class CodexEvent:
    """Normalised app-server notification.

    kind ∈ ``turn_started | turn_completed | item_completed | token_usage | thread_status |
    error``. ``params`` carries the raw JSON for anything the manager needs beyond the
    normalised fields. ``status`` for turn_completed: ``completed | interrupted | failed``.
    """

    kind: str
    thread_id: str | None
    turn_id: str | None
    status: str | None = None
    text: str | None = None  # final assistant text for turn_completed when available
    params: dict[str, object] = field(default_factory=dict)


class CodexClient(Protocol):
    """One websocket connection to one user's app-server."""

    async def thread_start(self, *, title: str | None = None) -> ThreadInfo: ...

    async def thread_resume(self, thread_id: str) -> ThreadInfo: ...

    async def thread_list(self, *, include_archived: bool = False) -> list[ThreadInfo]: ...

    async def thread_read(
        self, thread_id: str, *, cursor: str | None = None, limit: int = 20
    ) -> tuple[list[dict[str, object]], str | None]:
        """Return (items newest-last, next_cursor)."""
        ...

    async def thread_rename(self, thread_id: str, title: str) -> None: ...

    async def thread_archive(self, thread_id: str) -> None: ...

    async def thread_delete(self, thread_id: str) -> None: ...

    async def thread_fork(self, thread_id: str) -> ThreadInfo: ...

    async def thread_compact(self, thread_id: str) -> None: ...

    async def turn_start(self, thread_id: str, text: str) -> str:
        """Start a turn; return turn_id."""
        ...

    async def turn_steer(self, thread_id: str, turn_id: str, text: str) -> None: ...

    async def turn_interrupt(self, thread_id: str, turn_id: str) -> None: ...

    def events(self) -> AsyncIterator[CodexEvent]:
        """Notifications for this connection; ends when the connection closes."""
        ...

    async def close(self) -> None: ...


# opens + initialises a client for an endpoint
CodexConnector = Callable[[SandboxEndpoint], Awaitable[CodexClient]]


# ---------------------------------------------------------------- notifications (D, E -> H)

NoticeKind = Literal[
    "report",  # bridge report_progress
    "turn_completed",
    "turn_failed",
    "turn_interrupted",
    "queue_started",  # a queued instruction began running
    "queue_dropped",
]
ReportStatus = Literal["running", "waiting_user_input", "completed", "failed"]
FileKind = Literal["image", "audio", "video", "file"]


@dataclass(frozen=True, slots=True)
class StoredFile:
    """A file received through the bridge, parked in the bot's temp dir."""

    path: Path
    name: str
    mime: str
    size: int
    kind: FileKind
    url: str  # fetchable by NapCat: <public_base_url>/bridge/files/<token>/<name>


@dataclass(frozen=True, slots=True)
class CodingNotice:
    """Anything the user should hear about. The message text is never an LLM tool return."""

    kind: NoticeKind
    user_id: str
    thread_id: str | None
    title: str | None
    text: str
    status: ReportStatus | None = None
    files: tuple[StoredFile, ...] = ()


class CodingNotifier(Protocol):
    async def notify(self, notice: CodingNotice) -> None: ...


# ---------------------------------------------------------------- manager service API (D -> H)


@dataclass(frozen=True, slots=True)
class SubmitResult:
    thread_id: str
    state: Literal["started", "queued"]
    queue_position: int  # 0 when started


@dataclass(frozen=True, slots=True)
class ThreadStatus:
    thread_id: str
    title: str | None
    running: bool
    queued: int
    last_text: str | None
    updated_at: float


class CodingService(Protocol):
    """What the codex_* tools call. ``user_id`` always comes from the event, never the model."""

    async def submit(
        self, user_id: str, prompt: str, *, thread_id: str | None = None, title: str | None = None
    ) -> SubmitResult: ...

    async def list_threads(self, user_id: str) -> list[ThreadStatus]: ...

    async def peek(self, user_id: str, thread_id: str) -> ThreadStatus: ...

    async def read(
        self, user_id: str, thread_id: str, *, cursor: str | None = None, limit: int = 20
    ) -> tuple[list[dict[str, object]], str | None]: ...

    async def steer(self, user_id: str, thread_id: str, text: str) -> None: ...

    async def stop(self, user_id: str, thread_id: str, *, clear_queue: bool = False) -> int:
        """Interrupt the running turn; optionally drop queued instructions. Returns dropped."""
        ...

    async def compact(self, user_id: str, thread_id: str) -> None: ...

    async def rename(self, user_id: str, thread_id: str, title: str) -> None: ...

    async def archive(self, user_id: str, thread_id: str) -> None: ...

    async def delete(self, user_id: str, thread_id: str) -> None: ...

    async def fork(self, user_id: str, thread_id: str) -> SubmitResult: ...

    async def start(self) -> None: ...

    async def close(self) -> None: ...
