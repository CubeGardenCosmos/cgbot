"""The 15 omp_* ToolSpecs. Thin adapters over :class:`cgbot.omp.service.OmpService`.

Every tool is private-scope only and takes its owner from ``ToolContext.user_id``. Unknown
arguments are rejected. Results are JSON with saved passwords redacted; failures return
``is_error=True`` with a fixed, user-safe message.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING, Any

from cgbot.agent.tools import ToolContext, ToolResult, ToolSpec
from cgbot.omp.client import OmpError

if TYPE_CHECKING:
    from cgbot.omp.service import OmpService

logger = logging.getLogger(__name__)

PRIVATE_ONLY_MSG = "该工具仅限私聊 subagent 使用"
GENERIC_FAILURE_MSG = "omp 操作失败，请稍后重试。"
TEXT_MAX = 4000
MESSAGE_MAX = 20000
PATH_MAX = 1024
ID_MAX = 256

Operation = Callable[[str, "_Args"], Awaitable[dict[str, Any]]]


class _Args:
    """Typed access to one tool call's arguments. Rejects unknown keys and wrong types."""

    def __init__(self, raw: dict[str, Any], allowed: Iterable[str]) -> None:
        unknown = sorted(set(raw) - set(allowed))
        if unknown:
            raise OmpError(f"未知参数：{', '.join(unknown)}")
        self._raw = raw

    def text(
        self,
        name: str,
        *,
        default: str = "",
        required: bool = False,
        strip: bool = False,
        max_length: int = TEXT_MAX,
    ) -> str:
        value = self._raw.get(name, default)
        if value is None:
            value = default
        if not isinstance(value, str):
            raise OmpError(f"{name} 必须是文本。")
        if strip:
            value = value.strip()
        if required and not value.strip():
            raise OmpError(f"{name} 不能为空。")
        if len(value) > max_length:
            raise OmpError(f"{name} 过长。")
        return value

    def integer(self, name: str, default: int) -> int:
        value = self._raw.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int):
            raise OmpError(f"{name} 必须是整数。")
        return value

    def boolean(self, name: str, default: bool) -> bool:
        value = self._raw.get(name, default)
        if not isinstance(value, bool):
            raise OmpError(f"{name} 必须是布尔值。")
        return value

    def choice(self, name: str, default: str, options: tuple[str, ...]) -> str:
        value = self._raw.get(name, default)
        if not isinstance(value, str) or value not in options:
            raise OmpError(f"{name} 必须是 {' / '.join(options)} 之一。")
        return value


def _owner(ctx: ToolContext) -> str:
    if ctx.session_id != f"private:{ctx.user_id}":
        raise OmpError(PRIVATE_ONLY_MSG)
    return ctx.user_id


def _error_text(message: str) -> str:
    return json.dumps({"ok": False, "error": message}, ensure_ascii=False)


def _schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _s(description: str) -> dict[str, Any]:
    return {"type": "string", "description": description}


def _i(description: str) -> dict[str, Any]:
    return {"type": "integer", "description": description}


def _b(description: str) -> dict[str, Any]:
    return {"type": "boolean", "description": description}


def build_tools(svc: OmpService) -> list[ToolSpec]:
    """Return the 15 omp_* tools bound to ``svc``."""

    async def run(
        ctx: ToolContext, args: dict[str, Any], allowed: Iterable[str], op: Operation
    ) -> ToolResult:
        owner = ""
        try:
            owner = _owner(ctx)
            params = _Args(args, allowed)
            payload = await op(owner, params)
            return ToolResult(output=await svc.render(owner, payload))
        except OmpError as exc:
            return ToolResult(output=_error_text(exc.message), is_error=True)
        except Exception as exc:
            logger.warning("omp tool failed: %s", type(exc).__name__)
            return ToolResult(output=_error_text(GENERIC_FAILURE_MSG), is_error=True)

    def spec(
        name: str,
        description: str,
        properties: dict[str, Any],
        required: list[str],
        op: Operation,
    ) -> ToolSpec:
        allowed = tuple(properties)

        async def handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
            return await run(ctx, args, allowed, op)

        return ToolSpec(
            name=name,
            description=description,
            parameters_schema=_schema(properties, required),
            handler=handler,
        )

    async def add(owner: str, p: _Args) -> dict[str, Any]:
        port = p.integer("port", 0)
        return await svc.add_machine(
            owner,
            host=p.text("host", required=True, strip=True, max_length=PATH_MAX),
            port=port,
            password=p.text("password", required=True, max_length=ID_MAX),
            alias=p.text("alias", required=True, strip=True, max_length=ID_MAX),
        )

    async def delete(owner: str, p: _Args) -> dict[str, Any]:
        return await svc.delete_machine(owner, p.text("alias", required=True, strip=True))

    async def list_machines(owner: str, p: _Args) -> dict[str, Any]:
        return await svc.list_machines(owner)

    async def listdir(owner: str, p: _Args) -> dict[str, Any]:
        return await svc.listdir(
            owner,
            p.text("machine", strip=True),
            p.text("path", strip=True, max_length=PATH_MAX),
            p.text("keyword", strip=True),
        )

    async def create(owner: str, p: _Args) -> dict[str, Any]:
        return await svc.create_task(
            owner,
            machine=p.text("machine", strip=True),
            path=p.text("path", required=True, strip=True, max_length=PATH_MAX),
            prompt=p.text("prompt", max_length=MESSAGE_MAX),
        )

    async def tasks(owner: str, p: _Args) -> dict[str, Any]:
        return await svc.list_tasks(owner, p.text("machine", strip=True))

    async def sendprompt(owner: str, p: _Args) -> dict[str, Any]:
        return await svc.send_prompt(
            owner,
            p.text("task_id", required=True, strip=True, max_length=ID_MAX),
            p.text("message", required=True, max_length=MESSAGE_MAX),
            p.choice("mode", "auto", ("auto", "steer", "follow_up")),
        )

    async def steer(owner: str, p: _Args) -> dict[str, Any]:
        return await svc.steer(
            owner,
            p.text("task_id", required=True, strip=True, max_length=ID_MAX),
            p.text("message", required=True, max_length=MESSAGE_MAX),
        )

    async def stop(owner: str, p: _Args) -> dict[str, Any]:
        return await svc.stop(
            owner,
            p.text("task_id", required=True, strip=True, max_length=ID_MAX),
            p.boolean("clear_queue", False),
        )

    async def peek(owner: str, p: _Args) -> dict[str, Any]:
        return await svc.peek(
            owner,
            p.text("task_id", required=True, strip=True, max_length=ID_MAX),
            p.integer("thinking_chars", 1500),
        )

    async def read(owner: str, p: _Args) -> dict[str, Any]:
        return await svc.read(
            owner,
            p.text("task_id", required=True, strip=True, max_length=ID_MAX),
            limit=p.integer("limit", 20),
            cursor=p.text("cursor", strip=True, max_length=PATH_MAX),
            include_thinking=p.boolean("include_thinking", False),
            max_chars=p.integer("max_chars", 2000),
        )

    async def compact(owner: str, p: _Args) -> dict[str, Any]:
        return await svc.compact(
            owner,
            p.text("task_id", required=True, strip=True, max_length=ID_MAX),
            p.text("instructions", max_length=MESSAGE_MAX),
        )

    async def usage(owner: str, p: _Args) -> dict[str, Any]:
        return await svc.usage(
            owner,
            task_id=p.text("task_id", strip=True, max_length=ID_MAX),
            scope=p.choice("scope", "task", ("task", "account")),
            machine=p.text("machine", strip=True),
        )

    async def answer(owner: str, p: _Args) -> dict[str, Any]:
        return await svc.answer(
            owner,
            p.text("task_id", required=True, strip=True, max_length=ID_MAX),
            p.text("answer", required=True, max_length=MESSAGE_MAX),
            p.text("request_id", strip=True, max_length=ID_MAX),
        )

    async def report(owner: str, p: _Args) -> dict[str, Any]:
        return await svc.report(
            owner,
            task_id=p.text("task_id", strip=True, max_length=ID_MAX),
            mark_read=p.boolean("mark_read", True),
        )

    machine_prop = _s("机器别名；只有一台机器时可省略。")
    task_prop = _s("任务（ompweb 会话）ID，来自 omp_task_create 或 omp_task_list。")
    return [
        spec(
            "omp_add",
            "添加一台 ompweb 机器并保存密码（仅私聊）。密码只保存在本地数据库，不会回显。",
            {
                "host": _s("ompweb 主机名或 IP。"),
                "port": _i("ompweb 端口（1-65535）。"),
                "password": _s("ompweb 登录密码。"),
                "alias": _s("本地机器别名，用于后续工具引用。"),
            },
            ["host", "port", "password", "alias"],
            add,
        ),
        spec(
            "omp_del",
            "删除一台机器及其已跟踪任务；服务器上的会话不受影响，已有报告保留。",
            {"alias": _s("要删除的机器别名。")},
            ["alias"],
            delete,
        ),
        spec(
            "omp_list",
            "列出已添加的 ompweb 机器（不含密码）及各自已跟踪任务数。",
            {},
            [],
            list_machines,
        ),
        spec(
            "omp_task_listdir",
            "浏览机器上的目录与项目，用于选择任务工作目录。keyword 为模糊子序列过滤。",
            {
                "machine": machine_prop,
                "path": _s("要浏览的目录；留空则使用服务器默认位置。"),
                "keyword": _s("可选过滤关键词。"),
            },
            [],
            listdir,
        ),
        spec(
            "omp_task_create",
            "在指定目录创建 ompweb 任务；prompt 为空则只确保会话存在。",
            {
                "machine": machine_prop,
                "path": _s("任务工作目录（服务器上的绝对路径）。"),
                "prompt": _s("可选的首条指令。"),
            },
            ["path"],
            create,
        ),
        spec(
            "omp_task_list",
            "列出机器上正在运行的会话；其他用户跟踪的任务不会显示。",
            {"machine": machine_prop},
            [],
            tasks,
        ),
        spec(
            "omp_task_sendprompt",
            "向任务发送新指令。任务正忙时 auto 会排队为 follow-up；steer 仅用于忙碌中的即时干预。",
            {
                "task_id": task_prop,
                "message": _s("要发送的指令文本。"),
                "mode": _s("auto（默认）、steer 或 follow_up。"),
            },
            ["task_id", "message"],
            sendprompt,
        ),
        spec(
            "omp_task_steer",
            "在任务忙碌时即时干预当前执行；任务空闲时会被拒绝，请改用 omp_task_sendprompt。",
            {"task_id": task_prop, "message": _s("干预内容。")},
            ["task_id", "message"],
            steer,
        ),
        spec(
            "omp_task_stop",
            "中止任务当前执行；clear_queue=true 时同时取回排队中的指令文本。",
            {
                "task_id": task_prop,
                "clear_queue": _b("是否取回并清空排队指令（默认否）。"),
            },
            ["task_id"],
            stop,
        ),
        spec(
            "omp_task_peek",
            "查看任务当前阶段、思考尾部、助手文本尾部、待办、队列、子代理和待输入请求。",
            {
                "task_id": task_prop,
                "thinking_chars": _i("返回的思考文本字符数（0-10000，默认 1500）。"),
            },
            ["task_id"],
            peek,
        ),
        spec(
            "omp_task_read",
            "分页读取任务历史。首次不传 cursor；之后使用上一页返回的 next_cursor。",
            {
                "task_id": task_prop,
                "limit": _i("每页消息数（1-50，默认 20）。"),
                "cursor": _s("上一页返回的 next_cursor。"),
                "include_thinking": _b("是否包含思考内容（默认否）。"),
                "max_chars": _i("单条消息最大字符数（100-20000，默认 2000）。"),
            },
            ["task_id"],
            read,
        ),
        spec(
            "omp_task_compact",
            "压缩任务上下文；可附加压缩说明。返回压缩前后的 token 数与摘要。",
            {
                "task_id": task_prop,
                "instructions": _s("可选的压缩说明。"),
            },
            ["task_id"],
            compact,
        ),
        spec(
            "omp_task_usage",
            "查看用量。scope=task 查看任务上下文与子代理用量；scope=account 查看机器账户额度。",
            {
                "task_id": task_prop,
                "scope": _s("task（默认）或 account。"),
                "machine": machine_prop,
            },
            [],
            usage,
        ),
        spec(
            "omp_task_answer",
            "回答任务的待输入请求（ask/confirm/select/input/editor/open_url）。"
            "请先用 omp_task_report 或 omp_task_peek 查看请求。",
            {
                "task_id": task_prop,
                "answer": _s('回答内容；取消请传 {"cancelled": true} 的 JSON 文本。'),
                "request_id": _s("待回答请求 ID；留空则回答最近一个请求。"),
            },
            ["task_id", "answer"],
            answer,
        ),
        spec(
            "omp_task_report",
            "读取未读的后台任务报告（完成、错误、需要输入、退出、进度）。",
            {
                "task_id": _s("可选：仅读取该任务的报告。"),
                "mark_read": _b("读取后是否标记为已读（默认是）。"),
            },
            [],
            report,
        ),
    ]
