"""Model-callable ``codex_*`` tools (M5 R5.4), private chat only.

Identity is ``ToolContext.user_id`` and nothing else: no tool takes a user id, so the model cannot
act on another user's threads. Outputs are short and bounded; results of submitted work arrive
later through the notifier, so the model is told not to poll.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from cgbot.agent.tools import ToolContext, ToolHandler, ToolResult, ToolSpec
from cgbot.coding.contracts import CodingService, ThreadStatus
from cgbot.coding.manager import CodingError
from cgbot.memory.domain import PRIVATE_SESSION_PREFIX

MAX_OUTPUT_CHARS = 6000
MAX_PROMPT_CHARS = 8000
MAX_TITLE_CHARS = 80
MAX_THREAD_ID_CHARS = 128
MAX_CURSOR_CHARS = 512
MAX_LIST = 50
MAX_READ_LIMIT = 50
DEFAULT_READ_LIMIT = 10
ITEM_CHARS = 500
PREVIEW_CHARS = 200

SCOPE_MESSAGE = "Coding Agent tools are only available in the private chat"
_NOTE = (
    "Coding Agent 可以做任何事情（资料调研、数据收集、写代码、Playwright 浏览器、生成图片）；"
    "结果稍后以通知送达，请勿循环轮询。"
)

RunFn = Callable[[str, dict[str, Any]], Awaitable[ToolResult]]


class _Refused(Exception):
    """Argument problem; the message names the argument only, never its value."""


def _error(message: str) -> ToolResult:
    return ToolResult(output=message, is_error=True)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _bounded(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[: MAX_OUTPUT_CHARS - 20] + "\n…（输出已截断）"


def _required_text(args: dict[str, Any], key: str, *, max_chars: int) -> str:
    value = args.get(key)
    if not isinstance(value, str):
        raise _Refused(f"{key} is required and must be a string")
    text = value.strip()
    if not text:
        raise _Refused(f"{key} must not be empty")
    if len(text) > max_chars:
        raise _Refused(f"{key} is longer than {max_chars} characters")
    return text


def _optional_text(args: dict[str, Any], key: str, *, max_chars: int) -> str | None:
    if args.get(key) is None:
        return None
    value = args[key]
    if not isinstance(value, str):
        raise _Refused(f"{key} must be a string")
    text = value.strip()
    if len(text) > max_chars:
        raise _Refused(f"{key} is longer than {max_chars} characters")
    return text or None


def _flag(args: dict[str, Any], key: str) -> bool:
    value = args.get(key, False)
    if not isinstance(value, bool):
        raise _Refused(f"{key} must be a boolean")
    return value


def _read_limit(args: dict[str, Any]) -> int:
    value = args.get("limit", DEFAULT_READ_LIMIT)
    if not isinstance(value, int) or isinstance(value, bool):
        raise _Refused("limit must be an integer")
    if not 1 <= value <= MAX_READ_LIMIT:
        raise _Refused(f"limit must be between 1 and {MAX_READ_LIMIT}")
    return value


def _guarded(allowed: frozenset[str], run: RunFn) -> ToolHandler:
    """Common preamble: private session only, no unknown arguments, typed errors for the model."""

    async def handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        if ctx.session_id != f"{PRIVATE_SESSION_PREFIX}{ctx.user_id}":
            return _error(SCOPE_MESSAGE)
        try:
            unknown = sorted(set(args) - allowed)
            if unknown:
                raise _Refused(f"unknown argument(s): {', '.join(unknown)}")
            return await run(ctx.user_id, args)
        except (_Refused, CodingError) as exc:
            return _error(str(exc))

    return handler


def _status_line(status: ThreadStatus) -> str:
    state = "运行中" if status.running else "空闲"
    parts = [
        f"thread_id={status.thread_id}",
        f"标题={status.title or '(无)'}",
        state,
        f"排队={status.queued}",
    ]
    if status.last_text:
        parts.append(f"最近输出={_clip(status.last_text, PREVIEW_CHARS)}")
    return " | ".join(parts)


def _item_body(item: dict[str, object]) -> str:
    text = item.get("text")
    if isinstance(text, str) and text:
        return text
    content = item.get("content")
    if isinstance(content, list):
        pieces = [
            part["text"]
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        ]
        if pieces:
            return "\n".join(str(piece) for piece in pieces)
    return json.dumps(item, ensure_ascii=False, default=str)


def _render_item(item: dict[str, object]) -> str:
    label = item.get("type")
    return f"[{label if isinstance(label, str) else 'item'}] {_clip(_item_body(item), ITEM_CHARS)}"


_THREAD = {"type": "string", "description": "线程 ID（codex_list 返回）"}


def _schema(properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def _make_submit(service: CodingService) -> ToolHandler:
    async def run(user_id: str, args: dict[str, Any]) -> ToolResult:
        prompt = _required_text(args, "prompt", max_chars=MAX_PROMPT_CHARS)
        thread_id = _optional_text(args, "thread_id", max_chars=MAX_THREAD_ID_CHARS)
        title = _optional_text(args, "title", max_chars=MAX_TITLE_CHARS)
        result = await service.submit(user_id, prompt, thread_id=thread_id, title=title)
        if result.state == "started":
            state = "已开始"
        else:
            state = f"已排队（排队位置 {result.queue_position}）"
        return ToolResult(f"已提交 thread_id={result.thread_id}，{state}。结果稍后以通知送达。")

    return _guarded(frozenset({"prompt", "thread_id", "title"}), run)


def _make_list(service: CodingService) -> ToolHandler:
    async def run(user_id: str, args: dict[str, Any]) -> ToolResult:
        threads = await service.list_threads(user_id)
        if not threads:
            return ToolResult("没有 Coding Agent 线程。")
        lines = [_status_line(status) for status in threads[:MAX_LIST]]
        if len(threads) > MAX_LIST:
            lines.append(f"（另有 {len(threads) - MAX_LIST} 个线程未显示）")
        return ToolResult(_bounded("\n".join(lines)))

    return _guarded(frozenset(), run)


def _make_peek(service: CodingService) -> ToolHandler:
    async def run(user_id: str, args: dict[str, Any]) -> ToolResult:
        thread_id = _required_text(args, "thread_id", max_chars=MAX_THREAD_ID_CHARS)
        status = await service.peek(user_id, thread_id)
        return ToolResult(_bounded(_status_line(status)))

    return _guarded(frozenset({"thread_id"}), run)


def _make_read(service: CodingService) -> ToolHandler:
    async def run(user_id: str, args: dict[str, Any]) -> ToolResult:
        thread_id = _required_text(args, "thread_id", max_chars=MAX_THREAD_ID_CHARS)
        cursor = _optional_text(args, "cursor", max_chars=MAX_CURSOR_CHARS)
        limit = _read_limit(args)
        items, next_cursor = await service.read(user_id, thread_id, cursor=cursor, limit=limit)
        header = f"共 {len(items)} 条；next_cursor={next_cursor or '无'}"
        lines = [header, *(_render_item(item) for item in items)]
        return ToolResult(_bounded("\n".join(lines)))

    return _guarded(frozenset({"thread_id", "cursor", "limit"}), run)


def _make_steer(service: CodingService) -> ToolHandler:
    async def run(user_id: str, args: dict[str, Any]) -> ToolResult:
        thread_id = _required_text(args, "thread_id", max_chars=MAX_THREAD_ID_CHARS)
        text = _required_text(args, "text", max_chars=MAX_PROMPT_CHARS)
        await service.steer(user_id, thread_id, text)
        return ToolResult(f"已把指示追加到 thread_id={thread_id}。")

    return _guarded(frozenset({"thread_id", "text"}), run)


def _make_stop(service: CodingService) -> ToolHandler:
    async def run(user_id: str, args: dict[str, Any]) -> ToolResult:
        thread_id = _required_text(args, "thread_id", max_chars=MAX_THREAD_ID_CHARS)
        clear = _flag(args, "clear_queue")
        dropped = await service.stop(user_id, thread_id, clear_queue=clear)
        if clear:
            return ToolResult(f"已请求停止 thread_id={thread_id}，清空排队 {dropped} 条。")
        return ToolResult(f"已请求停止 thread_id={thread_id}。")

    return _guarded(frozenset({"thread_id", "clear_queue"}), run)


def _make_compact(service: CodingService) -> ToolHandler:
    async def run(user_id: str, args: dict[str, Any]) -> ToolResult:
        thread_id = _required_text(args, "thread_id", max_chars=MAX_THREAD_ID_CHARS)
        await service.compact(user_id, thread_id)
        return ToolResult(f"已请求压缩 thread_id={thread_id} 的上下文。")

    return _guarded(frozenset({"thread_id"}), run)


def _make_rename(service: CodingService) -> ToolHandler:
    async def run(user_id: str, args: dict[str, Any]) -> ToolResult:
        thread_id = _required_text(args, "thread_id", max_chars=MAX_THREAD_ID_CHARS)
        title = _required_text(args, "title", max_chars=MAX_TITLE_CHARS)
        await service.rename(user_id, thread_id, title)
        return ToolResult(f"已把 thread_id={thread_id} 改名为「{title}」。")

    return _guarded(frozenset({"thread_id", "title"}), run)


def _make_archive(service: CodingService) -> ToolHandler:
    async def run(user_id: str, args: dict[str, Any]) -> ToolResult:
        thread_id = _required_text(args, "thread_id", max_chars=MAX_THREAD_ID_CHARS)
        await service.archive(user_id, thread_id)
        return ToolResult(f"已归档 thread_id={thread_id}。")

    return _guarded(frozenset({"thread_id"}), run)


def _make_delete(service: CodingService) -> ToolHandler:
    async def run(user_id: str, args: dict[str, Any]) -> ToolResult:
        thread_id = _required_text(args, "thread_id", max_chars=MAX_THREAD_ID_CHARS)
        await service.delete(user_id, thread_id)
        return ToolResult(f"已删除 thread_id={thread_id}。")

    return _guarded(frozenset({"thread_id"}), run)


def _make_fork(service: CodingService) -> ToolHandler:
    async def run(user_id: str, args: dict[str, Any]) -> ToolResult:
        thread_id = _required_text(args, "thread_id", max_chars=MAX_THREAD_ID_CHARS)
        result = await service.fork(user_id, thread_id)
        return ToolResult(f"已分叉出新线程 thread_id={result.thread_id}（尚未运行）。")

    return _guarded(frozenset({"thread_id"}), run)


def codex_tools(service: CodingService) -> tuple[ToolSpec, ...]:
    """The eleven private-chat ``codex_*`` tools bound to ``service``."""
    return (
        ToolSpec(
            name="codex_submit",
            description=(
                "把任务交给 Coding Agent 执行。不传 thread_id 时新开线程；传入时在该线程继续，"
                "线程正在运行则指令排队。" + _NOTE
            ),
            parameters_schema=_schema(
                {
                    "prompt": {"type": "string", "description": "要执行的任务说明"},
                    "thread_id": _THREAD,
                    "title": {"type": "string", "description": "新线程的标题（可选）"},
                },
                required=("prompt",),
            ),
            handler=_make_submit(service),
        ),
        ToolSpec(
            name="codex_list",
            description="列出你的 Coding Agent 线程（标题、是否运行中、排队数、最近输出）。"
            + _NOTE,
            parameters_schema=_schema({}),
            handler=_make_list(service),
        ),
        ToolSpec(
            name="codex_peek",
            description="查看一个线程的当前状态。" + _NOTE,
            parameters_schema=_schema({"thread_id": _THREAD}, required=("thread_id",)),
            handler=_make_peek(service),
        ),
        ToolSpec(
            name="codex_read",
            description=(
                "分页读取线程历史条目（从旧到新）。cursor 取自上次返回的 next_cursor。" + _NOTE
            ),
            parameters_schema=_schema(
                {
                    "thread_id": _THREAD,
                    "cursor": {"type": "string", "description": "上次返回的 next_cursor（可选）"},
                    "limit": {
                        "type": "integer",
                        "description": f"条数，1-{MAX_READ_LIMIT}，默认 {DEFAULT_READ_LIMIT}",
                    },
                },
                required=("thread_id",),
            ),
            handler=_make_read(service),
        ),
        ToolSpec(
            name="codex_steer",
            description="向正在运行的线程追加一条指示（等待用户输入的任务用它回复）。" + _NOTE,
            parameters_schema=_schema(
                {"thread_id": _THREAD, "text": {"type": "string", "description": "指示内容"}},
                required=("thread_id", "text"),
            ),
            handler=_make_steer(service),
        ),
        ToolSpec(
            name="codex_stop",
            description="中断线程当前的运行；clear_queue=true 时同时清空排队指令。" + _NOTE,
            parameters_schema=_schema(
                {
                    "thread_id": _THREAD,
                    "clear_queue": {"type": "boolean", "description": "是否清空排队指令"},
                },
                required=("thread_id",),
            ),
            handler=_make_stop(service),
        ),
        ToolSpec(
            name="codex_compact",
            description="压缩线程的上下文以释放空间。" + _NOTE,
            parameters_schema=_schema({"thread_id": _THREAD}, required=("thread_id",)),
            handler=_make_compact(service),
        ),
        ToolSpec(
            name="codex_rename",
            description="修改线程标题。" + _NOTE,
            parameters_schema=_schema(
                {
                    "thread_id": _THREAD,
                    "title": {"type": "string", "description": "新标题"},
                },
                required=("thread_id", "title"),
            ),
            handler=_make_rename(service),
        ),
        ToolSpec(
            name="codex_archive",
            description="归档线程。" + _NOTE,
            parameters_schema=_schema({"thread_id": _THREAD}, required=("thread_id",)),
            handler=_make_archive(service),
        ),
        ToolSpec(
            name="codex_delete",
            description="删除线程（不可恢复）。" + _NOTE,
            parameters_schema=_schema({"thread_id": _THREAD}, required=("thread_id",)),
            handler=_make_delete(service),
        ),
        ToolSpec(
            name="codex_fork",
            description="从线程分叉出一个新线程（不运行新的轮次）。" + _NOTE,
            parameters_schema=_schema({"thread_id": _THREAD}, required=("thread_id",)),
            handler=_make_fork(service),
        ),
    )
