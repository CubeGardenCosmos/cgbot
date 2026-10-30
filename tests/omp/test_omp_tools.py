"""Tool surface: names, schemas, private-only scope, argument validation, safe error output."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from omp_fakes import FakeOmpweb, quiet_watchers

from cgbot.agent.tools import ToolContext, ToolResult, ToolSpec
from cgbot.model import Channel, OutboundMessage, SendResult
from cgbot.omp import omp_tools
from cgbot.omp.store import OmpStore

PW = "pw-secret-123"
PRIVATE_ONLY = "该工具仅限私聊 subagent 使用"
UNKNOWN_TASK = "未找到属于你的已跟踪任务"

EXPECTED_NAMES = {
    "omp_add",
    "omp_del",
    "omp_list",
    "omp_task_listdir",
    "omp_task_create",
    "omp_task_list",
    "omp_task_sendprompt",
    "omp_task_steer",
    "omp_task_stop",
    "omp_task_peek",
    "omp_task_read",
    "omp_task_compact",
    "omp_task_usage",
    "omp_task_answer",
    "omp_task_report",
}


async def _no_send(message: OutboundMessage) -> SendResult:
    return SendResult(ok=True)


def ctx_for(user_id: str = "u1", *, group: bool = False) -> ToolContext:
    session_id = f"group:{user_id}" if group else f"private:{user_id}"
    return ToolContext(
        user_id=user_id,
        session_id=session_id,
        source_channel=Channel(kind="qq", account=user_id),
        source_message_id=None,
        sender=_no_send,
    )


@pytest.fixture
async def tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[dict[str, ToolSpec]]:
    quiet_watchers(monkeypatch)
    store = OmpStore(tmp_path / "omp.sqlite")
    specs = omp_tools(store, http_client_factory=FakeOmpweb().factory())
    yield {spec.name: spec for spec in specs}
    store.close()


async def run(tools: dict[str, ToolSpec], name: str, args: dict, ctx: ToolContext) -> ToolResult:
    return await tools[name].handler(ctx, args)


def test_exposes_exactly_the_planned_tool_names(tools: dict[str, ToolSpec]) -> None:
    assert set(tools) == EXPECTED_NAMES
    assert len(tools) == 15


def test_every_schema_forbids_extra_arguments(tools: dict[str, ToolSpec]) -> None:
    for name, spec in tools.items():
        assert spec.parameters_schema.get("additionalProperties") is False, name


async def test_group_scope_is_refused_for_every_tool(tools: dict[str, ToolSpec]) -> None:
    for name in EXPECTED_NAMES:
        result = await run(tools, name, {}, ctx_for(group=True))
        assert result.is_error is True, name
        assert PRIVATE_ONLY in result.output, name


async def test_unknown_argument_is_rejected(tools: dict[str, ToolSpec]) -> None:
    result = await run(tools, "omp_list", {"user_id": "someone-else"}, ctx_for())
    assert result.is_error is True
    assert "未知参数" in result.output
    assert "user_id" in result.output


async def test_add_output_never_contains_password(tools: dict[str, ToolSpec]) -> None:
    args = {"host": "127.0.0.1", "port": 8080, "password": PW, "alias": "box"}
    result = await run(tools, "omp_add", args, ctx_for())
    assert result.is_error is False
    assert PW not in result.output


async def test_bad_port_returns_error_result_without_password(
    tools: dict[str, ToolSpec],
) -> None:
    args = {"host": "127.0.0.1", "port": 0, "password": PW, "alias": "box"}
    result = await run(tools, "omp_add", args, ctx_for())
    assert result.is_error is True
    assert PW not in result.output


async def test_zero_read_limit_is_reported_as_error(tools: dict[str, ToolSpec]) -> None:
    result = await run(tools, "omp_task_read", {"task_id": "x", "limit": 0}, ctx_for())
    assert result.is_error is True
    assert "limit 必须在 1 到 50 之间" in result.output


async def test_tasks_are_isolated_between_users(tools: dict[str, ToolSpec]) -> None:
    result = await run(tools, "omp_task_peek", {"task_id": "anything"}, ctx_for("someone-else"))
    assert result.is_error is True
    assert UNKNOWN_TASK in result.output
