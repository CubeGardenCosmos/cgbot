"""codex_btw: side progress question; thread resolution, bounds, error mapping."""

from __future__ import annotations

import pytest
from coding_app_fakes import FakeService, tool_ctx

from cgbot.agent.tools import ToolResult, ToolSpec
from cgbot.coding.contracts import ThreadStatus
from cgbot.coding.manager import CodingError
from cgbot.coding.tools import BTW_CLIP_CHARS, codex_tools


def _spec(service: FakeService) -> ToolSpec:
    return next(spec for spec in codex_tools(service) if spec.name == "codex_btw")


async def _btw(service: FakeService, args: dict[str, object]) -> ToolResult:
    return await _spec(service).handler(tool_ctx(), args)


def _status(thread_id: str, *, running: bool) -> ThreadStatus:
    return ThreadStatus(
        thread_id=thread_id,
        title=None,
        running=running,
        queued=0,
        last_text=None,
        updated_at=1.0,
    )


async def test_explicit_thread_is_asked_and_answer_is_returned() -> None:
    service = FakeService()
    result = await _btw(service, {"question": "进展如何", "thread_id": "t9"})
    assert not result.is_error
    assert "一切顺利" in result.output
    [(name, _, kwargs)] = service.calls
    assert name == "ask_progress"
    assert kwargs == {"thread_id": "t9", "question": "进展如何"}


async def test_omitted_thread_resolves_the_sole_running_thread() -> None:
    service = FakeService()
    service.threads = [_status("t1", running=True), _status("t2", running=False)]
    result = await _btw(service, {"question": "进展"})
    assert not result.is_error
    [(_, _, kwargs)] = [c for c in service.calls if c[0] == "ask_progress"]
    assert kwargs["thread_id"] == "t1"


async def test_no_running_thread_is_refused_without_asking() -> None:
    service = FakeService()
    service.threads = [_status("t1", running=False)]
    result = await _btw(service, {"question": "进展"})
    assert result.is_error
    assert all(name != "ask_progress" for name, _, _ in service.calls)


async def test_several_running_threads_require_explicit_thread_id() -> None:
    service = FakeService()
    service.threads = [_status("t1", running=True), _status("t2", running=True)]
    result = await _btw(service, {"question": "进展"})
    assert result.is_error
    assert all(name != "ask_progress" for name, _, _ in service.calls)


async def test_long_answer_is_clipped_to_the_btw_bound() -> None:
    service = FakeService()
    service.progress_text = "字" * (BTW_CLIP_CHARS * 3)
    result = await _btw(service, {"question": "进展", "thread_id": "t1"})
    assert result.output.endswith("…")
    assert len(result.output) < BTW_CLIP_CHARS + 100


async def test_coding_error_is_surfaced_as_a_tool_error() -> None:
    service = FakeService()
    service.error = CodingError("progress check timed out")
    result = await _btw(service, {"question": "进展", "thread_id": "t1"})
    assert result.is_error
    assert result.output == "progress check timed out"


async def test_unknown_argument_is_refused() -> None:
    service = FakeService()
    result = await _btw(service, {"question": "进展", "prompt": "x"})
    assert result.is_error
    assert "prompt" in result.output


@pytest.mark.parametrize("question", ["", "   "])
async def test_blank_question_is_refused(question: str) -> None:
    result = await _btw(FakeService(), {"question": question})
    assert result.is_error
