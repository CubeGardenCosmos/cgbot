"""Per-task watcher: polls session state and follows the SSE stream, then records reports.

One asyncio task per tracked session. Each iteration combines a poll (every ``poll_interval``)
with the SSE stream. Whichever side fails first cancels the other, then the loop backs off
(1s, doubling, capped at 30s). A failure in one task never affects other tasks.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import TYPE_CHECKING, Any

from cgbot.omp import api
from cgbot.omp.client import OmpClient
from cgbot.omp.store import TrackedTask

if TYPE_CHECKING:
    from cgbot.omp.service import OmpService

logger = logging.getLogger(__name__)

BACKOFF_START_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 30.0
SSE_EVENT_TYPES = frozenset(
    ("session_settled", "prompt_error", "extension_ui_request", "agent_start")
)
UI_METHODS_NEEDING_INPUT = frozenset(("select", "confirm", "input", "editor", "ask", "open_url"))


class TaskWatch:
    """Observes one tracked task and records reports through the owning service."""

    def __init__(self, service: OmpService, task: TrackedTask) -> None:
        self._svc = service
        self._task = task

    async def run(self) -> None:
        delay = BACKOFF_START_SECONDS
        while not self._svc.closed:
            poll: asyncio.Task[None] | None = None
            try:
                client = await self._svc.client_for(self._task.owner, self._task.alias)
                await self.observe(client)
                poll = asyncio.create_task(self._poll(client))
                stream = asyncio.create_task(self._consume(client))
                try:
                    done, _ = await asyncio.wait(
                        {poll, stream}, return_when=asyncio.FIRST_COMPLETED
                    )
                    for finished in done:
                        finished.result()
                finally:
                    stream.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await stream
                delay = BACKOFF_START_SECONDS
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Log the exception type only: its text may carry URLs or bodies.
                logger.warning("omp watch retry for one task: %s", type(exc).__name__)
            finally:
                if poll is not None:
                    poll.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await poll
            if self._svc.closed:
                return
            await asyncio.sleep(delay)
            delay = min(BACKOFF_CAP_SECONDS, delay * 2)

    async def _poll(self, client: OmpClient) -> None:
        while True:
            await asyncio.sleep(self._svc.poll_interval)
            await self.observe(client)

    async def _consume(self, client: OmpClient) -> None:
        async for event in client.events(self._task.task_id):
            await self.handle_event(client, event)

    async def observe(self, client: OmpClient) -> None:
        task = self._task
        snapshot = await api.fetch_state(client, task.task_id)
        state = snapshot["state"]
        if not snapshot["running"]:
            if task.process_seen:
                await self._record("exited", "任务进程已退出", None)
                task.process_seen = False
                await self._svc.save_task(task)
            return
        task.process_seen = True
        is_busy = api.busy(state)
        if is_busy and not task.failed:
            task.active = True
        elif task.active and not task.waiting_input and not api.queued(state):
            await self.completed(client)
        interval = self._svc.progress_interval
        now = time.time()
        if is_busy and interval and now - task.last_progress_at >= interval:
            task.last_progress_at = now
            await self._record(
                "progress",
                "任务仍在运行",
                {
                    k: state.get(k)
                    for k in ("todoPhases", "contextUsage", "isStreaming", "isCompacting")
                },
            )

    async def completed(self, client: OmpClient) -> None:
        task = self._task
        if not task.active or task.failed or task.waiting_input:
            return
        snapshot = await api.fetch_state(client, task.task_id)
        state = snapshot["state"]
        if (
            not snapshot["running"]
            or api.busy(state)
            or api.queued(state)
            or not task.active
            or task.failed
            or task.waiting_input
        ):
            return
        result = await client.post(
            api.command_path(task.task_id), {"type": "get_last_assistant_text"}
        )
        if not task.active or task.failed or task.waiting_input:
            return
        usage = state.get("contextUsage")
        percent = usage.get("percent") if isinstance(usage, dict) else None
        await self._record(
            "completed",
            "任务已完成",
            result.get("data"),
            context_percent=percent if isinstance(percent, (int, float)) else None,
        )
        task.active = False
        task.waiting_input = False
        await self._svc.save_task(task)

    async def handle_event(self, client: OmpClient, event: dict[str, Any]) -> None:
        task = self._task
        kind = event.get("type")
        if kind not in SSE_EVENT_TYPES:
            return
        payload = {k: v for k, v in event.items() if k != "web"}
        if kind == "session_settled":
            await self.completed(client)
        elif kind == "prompt_error":
            task.active = False
            task.failed = True
            await self._record("error", "任务发生错误", payload)
        elif kind == "extension_ui_request" and payload.get("method") in UI_METHODS_NEEDING_INPUT:
            task.waiting_input = True
            task.pending_ui[str(payload.get("id"))] = payload
            await self._record("needs_input", "任务需要输入或确认", payload)
        elif kind == "extension_ui_request" and payload.get("method") == "cancel":
            task.pending_ui.pop(str(payload.get("targetId", "")), None)
            task.waiting_input = bool(task.pending_ui)
        elif kind == "agent_start":
            task.active = not task.failed
            task.waiting_input = bool(task.pending_ui)
        await self._svc.save_task(task)

    async def _record(
        self,
        kind: str,
        summary: str,
        detail: Any,
        *,
        context_percent: float | None = None,
    ) -> None:
        await self._svc.record_report(
            self._task, kind, summary, detail, context_percent=context_percent
        )
