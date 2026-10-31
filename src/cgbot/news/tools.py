"""Model-callable ``news_subscribe`` and ``news_fetch`` tools for 世界信号.

Both tools are private-only: a session must be exactly the caller's own ``private:<user_id>``.
The user is always ``ToolContext.user_id``; model arguments never name another user.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Final

from cgbot.agent.tools import ToolContext, ToolResult, ToolSpec
from cgbot.memory.domain import PRIVATE_SESSION_PREFIX
from cgbot.news.delivery import TIER_NAMES, build_issue_messages, read_delivery
from cgbot.news.pusher import GAP_SECONDS, Clock, TargetOf, send_groups
from cgbot.news.store import NewsStore

NEWS_SUBSCRIBE_TOOL_NAME: Final = "news_subscribe"
NEWS_FETCH_TOOL_NAME: Final = "news_fetch"
SUBSCRIBE_ACTIONS: Final = ("subscribe", "unsubscribe", "status")
FETCH_TIERS: Final = ("fast", "lite", "max", "all")
_PRIVATE_ONLY: Final = "世界信号只能在和用户的私聊中办理。"

_SUBSCRIBE_DESCRIPTION = (
    "管理每日「世界信号」新闻摘要的订阅(私聊专用)。"
    "何时用:用户要订阅、取消订阅或查询世界信号订阅状态时调用。"
    "订阅后每天定时把当天的刊推送到本私聊。"
)
_FETCH_DESCRIPTION = (
    "把今天的「世界信号」刊立即发到本私聊(私聊专用,只读,不触发生成)。"
    "何时用:用户要求现在就取/看今天的世界信号时调用。"
    "今天的刊还没生成完成时会如实告知,不会发送任何内容。"
)


def _error(message: str) -> ToolResult:
    return ToolResult(output=message, is_error=True)


def _is_private(ctx: ToolContext) -> bool:
    return ctx.session_id == f"{PRIVATE_SESSION_PREFIX}{ctx.user_id}"


def news_subscribe_tool(store: NewsStore, push_time: str) -> ToolSpec:
    async def handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        if not _is_private(ctx):
            return _error(_PRIVATE_ONLY)
        unknown = sorted(set(args) - {"action"})
        if unknown:
            return _error(f"unknown argument(s): {', '.join(unknown)}")
        action = args.get("action")
        if action not in SUBSCRIBE_ACTIONS:
            return _error("action must be one of: subscribe, unsubscribe, status")
        if action == "subscribe":
            added = await store.subscribe(ctx.user_id)
            text = (
                f"已订阅世界信号,每天 {push_time} 左右推送到这里。"
                if added
                else "你已经订阅过世界信号了。"
            )
        elif action == "unsubscribe":
            removed = await store.unsubscribe(ctx.user_id)
            text = "已取消订阅世界信号。" if removed else "你本来就没有订阅世界信号。"
        else:
            subscribed = await store.is_subscribed(ctx.user_id)
            text = (
                f"已订阅世界信号,每天 {push_time} 左右推送。" if subscribed else "未订阅世界信号。"
            )
        return ToolResult(output=text, is_error=False)

    return ToolSpec(
        name=NEWS_SUBSCRIBE_TOOL_NAME,
        description=_SUBSCRIBE_DESCRIPTION,
        parameters_schema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": list(SUBSCRIBE_ACTIONS),
                    "description": "subscribe=订阅; unsubscribe=取消订阅; status=查询订阅状态",
                },
            },
            "required": ["action"],
        },
        handler=handler,
    )


def news_fetch_tool(
    output_dir: Path,
    target_of: TargetOf,
    clock: Clock,
    gap_seconds: float = GAP_SECONDS,
) -> ToolSpec:
    async def handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        if not _is_private(ctx):
            return _error(_PRIVATE_ONLY)
        unknown = sorted(set(args) - {"tier"})
        if unknown:
            return _error(f"unknown argument(s): {', '.join(unknown)}")
        tier = args.get("tier", "all")
        if tier not in FETCH_TIERS:
            return _error("tier must be one of: fast, lite, max, all")
        day = clock().date().isoformat()
        delivery = await asyncio.to_thread(read_delivery, output_dir, day)
        if delivery is None:
            return ToolResult(output=f"今天({day})的世界信号还没有生成,稍后再试。", is_error=False)
        if not delivery.complete:
            return ToolResult(
                output="今天的世界信号还没生成完成,暂时不能取,稍后再试。", is_error=False
            )
        tiers = TIER_NAMES if tier == "all" else (tier,)
        built = build_issue_messages(delivery, tiers)
        if not built.groups:
            return ToolResult(
                output="你要的世界信号暂时不可用:" + ";".join(built.notes),
                is_error=False,
            )
        account = await target_of(ctx.user_id)
        ok = await send_groups(ctx.sender, account, built.groups, gap_seconds=gap_seconds)
        if not ok:
            return _error("世界信号发送失败,请稍后再试。")
        note = f"未附上:{';'.join(built.notes)}" if built.notes else ""
        return ToolResult(output=f"已把今天的世界信号({delivery.issue_id})发到本私聊。{note}")

    return ToolSpec(
        name=NEWS_FETCH_TOOL_NAME,
        description=_FETCH_DESCRIPTION,
        parameters_schema={
            "type": "object",
            "properties": {
                "tier": {
                    "type": "string",
                    "enum": list(FETCH_TIERS),
                    "description": "fast=快讯长图; lite=精简版 PDF; max=完整版链接; all=全部(默认)",
                },
            },
        },
        handler=handler,
    )
