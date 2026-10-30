"""TaskWatch transitions (completed, exited, errors, input requests, progress) and wake delivery.

Transitions are driven directly through ``observe`` / ``handle_event`` against ``FakeOmpweb``;
no background loop runs (``quiet_watchers``), so the tests do not depend on timing.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from omp_fakes import FakeOmpweb, quiet_watchers

from cgbot.omp.client import OmpClient
from cgbot.omp.service import WAKE_PREFIX, OmpService
from cgbot.omp.store import OmpStore
from cgbot.omp.watch import TaskWatch

OWNER = "u1"
PW = "pw-secret-123"


class Harness:
    def __init__(self, service: OmpService, store: OmpStore, fake: FakeOmpweb) -> None:
        self.service = service
        self.store = store
        self.fake = fake

    async def add_box(self) -> None:
        await self.service.add_machine(OWNER, host="127.0.0.1", port=8080, password=PW, alias="box")

    async def start_task(self, prompt: str = "go") -> str:
        created = await self.service.create_task(OWNER, machine="box", path="/w", prompt=prompt)
        return created["task_id"]

    async def watch(self, task_id: str) -> tuple[TaskWatch, OmpClient]:
        task = self.service._task_record(OWNER, task_id)
        client = await self.service.client_for(OWNER, "box")
        return TaskWatch(self.service, task), client

    async def reports(self) -> list[dict]:
        return (await self.service.report(OWNER, task_id="", mark_read=False))["reports"]


@pytest.fixture
async def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Harness]:
    quiet_watchers(monkeypatch)
    store = OmpStore(tmp_path / "omp.sqlite")
    fake = FakeOmpweb()
    service = OmpService(store, http_client_factory=fake.factory(), wake_window_sec=0.01)
    h = Harness(service, store, fake)
    await h.add_box()
    yield h
    await service.close()
    store.close()


async def test_completion_is_reported_once_with_last_text(harness: Harness) -> None:
    task_id = await harness.start_task()
    session = harness.fake.sessions[task_id]
    session.last_text = "all done"
    session.streaming = False
    watch, client = await harness.watch(task_id)

    await watch.observe(client)
    await watch.observe(client)

    reports = await harness.reports()
    assert [(r["kind"], r["detail"]) for r in reports] == [("completed", "all done")]


async def test_session_settled_event_completes_the_task(harness: Harness) -> None:
    task_id = await harness.start_task()
    harness.fake.sessions[task_id].last_text = "finished"
    harness.fake.sessions[task_id].streaming = False
    watch, client = await harness.watch(task_id)

    await watch.handle_event(client, {"type": "session_settled"})

    assert [r["kind"] for r in await harness.reports()] == ["completed"]


async def test_no_completion_while_follow_up_is_queued(harness: Harness) -> None:
    task_id = await harness.start_task()
    session = harness.fake.sessions[task_id]
    session.streaming = False
    session.queued_follow_up.append("next")
    watch, client = await harness.watch(task_id)

    await watch.observe(client)

    assert await harness.reports() == []
    assert watch._task.active is True


async def test_process_exit_is_reported_once(harness: Harness) -> None:
    task_id = await harness.start_task()
    watch, client = await harness.watch(task_id)
    harness.service.progress_interval = 0.0  # keep the exit test free of progress reports
    await watch.observe(client)  # running and busy: marks the process as seen
    harness.fake.sessions[task_id].running = False

    await watch.observe(client)
    await watch.observe(client)

    assert [r["kind"] for r in await harness.reports()] == ["exited"]


async def test_prompt_error_marks_failed_and_suppresses_completion(harness: Harness) -> None:
    task_id = await harness.start_task()
    harness.fake.sessions[task_id].streaming = False
    watch, client = await harness.watch(task_id)

    await watch.handle_event(
        client, {"type": "prompt_error", "message": "model down", "web": {"x": 1}}
    )
    await watch.handle_event(client, {"type": "session_settled"})

    reports = await harness.reports()
    assert [r["kind"] for r in reports] == ["error"]
    assert "web" not in reports[0]["detail"]
    assert watch._task.failed is True


async def test_select_request_needs_input_and_answer_round_trip(harness: Harness) -> None:
    task_id = await harness.start_task()
    watch, client = await harness.watch(task_id)
    event = {"type": "extension_ui_request", "id": "r1", "method": "select", "options": ["A", "B"]}

    await watch.handle_event(client, event)

    assert watch._task.waiting_input is True
    assert "r1" in watch._task.pending_ui
    assert [r["kind"] for r in await harness.reports()] == ["needs_input"]

    await harness.service.answer(OWNER, task_id, "A", "r1")
    assert harness.fake.commands[-1][1] == {
        "type": "extension_ui_response",
        "id": "r1",
        "value": "A",
    }
    assert watch._task.pending_ui == {}


async def test_cancel_event_removes_pending_request(harness: Harness) -> None:
    task_id = await harness.start_task()
    watch, client = await harness.watch(task_id)
    await watch.handle_event(
        client,
        {"type": "extension_ui_request", "id": "r1", "method": "confirm"},
    )
    await watch.handle_event(
        client,
        {"type": "extension_ui_request", "method": "cancel", "targetId": "r1"},
    )
    assert watch._task.pending_ui == {}
    assert watch._task.waiting_input is False


async def test_progress_report_is_recorded_while_busy(harness: Harness) -> None:
    task_id = await harness.start_task()
    harness.fake.sessions[task_id].streaming = True
    harness.service.progress_interval = 1e-9
    watch, client = await harness.watch(task_id)

    await watch.observe(client)

    kinds = [r["kind"] for r in await harness.reports()]
    assert kinds == ["progress"]


async def test_wake_delivers_prefixed_batch_then_clears_pending(harness: Harness) -> None:
    delivered: list[tuple[str, str]] = []

    async def on_wake(owner: str, text: str) -> None:
        delivered.append((owner, text))

    harness.service._on_wake = on_wake
    task_id = await harness.start_task(prompt="")
    task = harness.service._task_record(OWNER, task_id)
    await harness.service.record_report(task, "error", "boom", None, context_percent=None)

    await asyncio.wait_for(harness.service._wakes[OWNER], timeout=2)

    [(owner, text)] = delivered
    assert owner == OWNER
    assert text.startswith(WAKE_PREFIX)
    body = json.loads(text[len(WAKE_PREFIX) :])
    assert [item["kind"] for item in body] == ["error"]
    assert await harness.store.pending_notifications(OWNER) == []


async def test_failed_wake_keeps_reports_pending(harness: Harness) -> None:
    async def broken(owner: str, text: str) -> None:
        raise RuntimeError("delivery down")

    harness.service._on_wake = broken
    task_id = await harness.start_task(prompt="")
    task = harness.service._task_record(OWNER, task_id)
    await harness.service.record_report(task, "error", "boom", None, context_percent=None)

    await asyncio.wait_for(harness.service._wakes[OWNER], timeout=2)

    pending = await harness.store.pending_notifications(OWNER)
    assert [r.summary for r in pending] == ["boom"]
