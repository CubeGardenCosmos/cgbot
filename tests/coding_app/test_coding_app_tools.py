"""codex_* tools: scope, identity, argument validation, output bounds, error mapping."""

from __future__ import annotations

import pytest
from coding_app_fakes import FakeService, tool_ctx

from cgbot.agent.tools import ToolResult, ToolSpec
from cgbot.coding.contracts import SubmitResult, ThreadStatus
from cgbot.coding.manager import CodingError
from cgbot.coding.tools import (
    DEFAULT_READ_LIMIT,
    MAX_LIST,
    MAX_OUTPUT_CHARS,
    MAX_PROMPT_CHARS,
    MAX_TITLE_CHARS,
    SCOPE_MESSAGE,
    codex_tools,
)

EXPECTED_NAMES = {
    "codex_submit",
    "codex_list",
    "codex_peek",
    "codex_read",
    "codex_steer",
    "codex_stop",
    "codex_compact",
    "codex_rename",
    "codex_archive",
    "codex_delete",
    "codex_fork",
    "codex_btw",
}


def _spec(service: FakeService, name: str) -> ToolSpec:
    return next(spec for spec in codex_tools(service) if spec.name == name)


async def _call(
    service: FakeService, name: str, args: dict[str, object], *, group: bool = False
) -> ToolResult:
    spec = _spec(service, name)
    return await spec.handler(tool_ctx(group=group), args)


def test_exposes_twelve_tools_with_closed_schemas_and_no_polling_note() -> None:
    specs = codex_tools(FakeService())
    assert {spec.name for spec in specs} == EXPECTED_NAMES
    for spec in specs:
        assert spec.parameters_schema["type"] == "object"
        assert spec.parameters_schema["additionalProperties"] is False
        assert "请勿循环轮询" in spec.description
        assert spec.channel_param is None


@pytest.mark.parametrize("name", sorted(EXPECTED_NAMES))
async def test_group_session_is_refused_before_service_is_touched(name: str) -> None:
    service = FakeService()
    result = await _call(service, name, {"thread_id": "t1", "prompt": "x"}, group=True)
    assert result.is_error
    assert result.output == SCOPE_MESSAGE
    assert service.calls == []


async def test_identity_comes_from_context_and_user_id_argument_is_refused() -> None:
    service = FakeService()
    ok = await _call(service, "codex_list", {})
    assert not ok.is_error
    assert service.calls[-1][1] == "1001"

    bad = await _call(service, "codex_peek", {"thread_id": "t1", "user_id": "9999"})
    assert bad.is_error
    assert "unknown argument(s): user_id" in bad.output
    assert "9999" not in bad.output
    assert [name for name, _, _ in service.calls] == ["list_threads"]


async def test_submit_strips_prompt_and_reports_started() -> None:
    service = FakeService()
    result = await _call(service, "codex_submit", {"prompt": "  写报告  ", "title": " 论文 "})
    assert not result.is_error
    name, user_id, kwargs = service.calls[0]
    assert (name, user_id) == ("submit", "1001")
    assert kwargs == {
        "prompt": "写报告",
        "thread_id": None,
        "title": "论文",
        "important": False,
        "time_limit_minutes": None,
    }
    assert "thread_id=t1" in result.output
    assert "已开始" in result.output


async def test_important_budget_arguments_reach_service() -> None:
    service = FakeService()
    args = {"prompt": "x", "important": True, "time_limit_minutes": 5}
    result = await _call(service, "codex_submit", args)
    assert not result.is_error
    kwargs = service.calls[0][2]
    assert kwargs["important"] is True
    assert kwargs["time_limit_minutes"] == 5.0


async def test_blank_thread_id_on_submit_means_new_thread() -> None:
    service = FakeService()
    await _call(service, "codex_submit", {"prompt": "x", "thread_id": "   "})
    assert service.calls[0][2]["thread_id"] is None


async def test_submit_reports_queue_position_when_queued() -> None:
    service = FakeService()
    service.submit_result = SubmitResult(thread_id="t1", state="queued", queue_position=2)
    result = await _call(service, "codex_submit", {"prompt": "x", "thread_id": "t1"})
    assert "已排队（排队位置 2）" in result.output


@pytest.mark.parametrize(
    ("args", "fragment"),
    [
        ({}, "prompt is required"),
        ({"prompt": "   "}, "prompt must not be empty"),
        ({"prompt": "x" * (MAX_PROMPT_CHARS + 1)}, "prompt is longer"),
        ({"prompt": 5}, "prompt is required"),
        ({"prompt": "x", "title": "t" * (MAX_TITLE_CHARS + 1)}, "title is longer"),
        ({"prompt": "x", "important": "yes"}, "important must be a boolean"),
        ({"prompt": "x", "important": True, "time_limit_minutes": "5"}, "must be a number"),
        ({"prompt": "x", "important": True, "time_limit_minutes": True}, "must be a number"),
        ({"prompt": "x", "important": True, "time_limit_minutes": 0}, "must be a positive"),
    ],
)
async def test_submit_argument_validation(args: dict[str, object], fragment: str) -> None:
    service = FakeService()
    result = await _call(service, "codex_submit", args)
    assert result.is_error
    assert fragment in result.output
    assert service.calls == []


async def test_list_empty_and_rendered_status_lines() -> None:
    service = FakeService()
    service.threads = []
    empty = await _call(service, "codex_list", {})
    assert empty.output == "没有 Coding Agent 线程。"

    service.threads = [
        ThreadStatus(
            thread_id="t1",
            title="论文",
            running=True,
            queued=1,
            last_text="进展",
            updated_at=1.0,
        )
    ]
    listed = await _call(service, "codex_list", {})
    assert "thread_id=t1" in listed.output
    assert "标题=论文" in listed.output
    assert "运行中" in listed.output
    assert "排队=1" in listed.output
    assert "最近输出=进展" in listed.output


async def test_list_caps_the_number_of_threads_shown() -> None:
    service = FakeService()
    service.threads = [
        ThreadStatus(
            thread_id=f"t{i}",
            title=None,
            running=False,
            queued=0,
            last_text=None,
            updated_at=float(i),
        )
        for i in range(MAX_LIST + 10)
    ]
    result = await _call(service, "codex_list", {})
    assert "（另有 10 个线程未显示）" in result.output
    assert result.output.count("thread_id=") == MAX_LIST


async def test_peek_renders_status_line() -> None:
    service = FakeService()
    result = await _call(service, "codex_peek", {"thread_id": "t1"})
    assert not result.is_error
    assert "标题=论文" in result.output
    assert service.calls[0][2] == {"thread_id": "t1"}


async def test_thread_id_is_required_where_declared() -> None:
    service = FakeService()
    result = await _call(service, "codex_peek", {})
    assert result.is_error
    assert "thread_id is required" in result.output
    assert service.calls == []


async def test_read_defaults_and_renders_header_and_items() -> None:
    service = FakeService()
    result = await _call(service, "codex_read", {"thread_id": "t1"})
    assert not result.is_error
    assert service.calls[0][2] == {"thread_id": "t1", "cursor": None, "limit": DEFAULT_READ_LIMIT}
    assert result.output.startswith("共 1 条；next_cursor=c2")
    assert "[agentMessage] 结果" in result.output


@pytest.mark.parametrize("limit", [0, 51, "5", True, 2.5])
async def test_read_limit_validation(limit: object) -> None:
    service = FakeService()
    result = await _call(service, "codex_read", {"thread_id": "t1", "limit": limit})
    assert result.is_error
    assert "limit must be" in result.output
    assert service.calls == []


async def test_read_renders_content_parts_and_falls_back_to_json() -> None:
    service = FakeService()
    service.items = [
        {"type": "reasoning", "content": [{"text": "思考一下"}]},
        {"type": "tool", "id": "x"},
    ]
    result = await _call(service, "codex_read", {"thread_id": "t1"})
    assert "[reasoning] 思考一下" in result.output
    assert '"id": "x"' in result.output


async def test_read_output_is_bounded() -> None:
    service = FakeService()
    service.items = [{"type": "agentMessage", "text": "长" * 900} for _ in range(50)]
    result = await _call(service, "codex_read", {"thread_id": "t1", "limit": 50})
    assert len(result.output) <= MAX_OUTPUT_CHARS
    assert "输出已截断" in result.output


async def test_steer_strips_text_and_acknowledges() -> None:
    service = FakeService()
    result = await _call(service, "codex_steer", {"thread_id": "t1", "text": " 选方案 A "})
    assert service.calls[0][2] == {"thread_id": "t1", "text": "选方案 A"}
    assert "已把指示追加到 thread_id=t1" in result.output


async def test_stop_reports_dropped_when_clearing_queue() -> None:
    service = FakeService()
    plain = await _call(service, "codex_stop", {"thread_id": "t1"})
    assert plain.output == "已请求停止 thread_id=t1。"
    cleared = await _call(service, "codex_stop", {"thread_id": "t1", "clear_queue": True})
    assert service.calls[-1][2] == {"thread_id": "t1", "clear_queue": True}
    assert "清空排队 2 条" in cleared.output


async def test_stop_clear_queue_must_be_boolean() -> None:
    service = FakeService()
    result = await _call(service, "codex_stop", {"thread_id": "t1", "clear_queue": "yes"})
    assert result.is_error
    assert "clear_queue must be a boolean" in result.output
    assert service.calls == []


async def test_rename_rejects_title_over_limit() -> None:
    service = FakeService()
    result = await _call(
        service, "codex_rename", {"thread_id": "t1", "title": "t" * (MAX_TITLE_CHARS + 1)}
    )
    assert result.is_error
    assert service.calls == []


@pytest.mark.parametrize(
    ("name", "args", "call_name", "fragment"),
    [
        ("codex_rename", {"thread_id": "t1", "title": "新名"}, "rename", "改名为「新名」"),
        ("codex_compact", {"thread_id": "t1"}, "compact", "压缩"),
        ("codex_archive", {"thread_id": "t1"}, "archive", "已归档"),
        ("codex_delete", {"thread_id": "t1"}, "delete", "已删除"),
        ("codex_fork", {"thread_id": "t1"}, "fork", "分叉出新线程 thread_id=t1"),
    ],
)
async def test_lifecycle_tools_call_service_with_user_identity(
    name: str, args: dict[str, object], call_name: str, fragment: str
) -> None:
    service = FakeService()
    result = await _call(service, name, args)
    assert not result.is_error
    assert service.calls[0][0] == call_name
    assert service.calls[0][1] == "1001"
    assert fragment in result.output


async def test_coding_error_becomes_error_result_with_its_message() -> None:
    service = FakeService()
    service.error = CodingError("线程不存在")
    result = await _call(service, "codex_peek", {"thread_id": "t404"})
    assert result.is_error
    assert result.output == "线程不存在"


async def test_error_output_never_echoes_argument_values() -> None:
    service = FakeService()
    secret_prompt = "super-secret-prompt-body"
    result = await _call(service, "codex_submit", {"prompt": secret_prompt, "thread_id": 7})
    assert result.is_error
    assert secret_prompt not in result.output
    assert result.output == "thread_id must be a string"
