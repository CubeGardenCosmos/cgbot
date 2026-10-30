"""Model-callable web search tools: ``web_search`` plus, when enabled, ``gemini_search``.

Arguments are validated against the schema here (unknown keys rejected, count and freshness
bounded) before reaching :class:`SearchService`. Results are rendered as short Chinese text lines;
failures carry a stable code followed by a neutral explanation, never a remote body.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from cgbot.agent.tools import ToolContext, ToolResult, ToolSpec
from cgbot.config.settings import SearchGeminiConfig
from cgbot.search.models import (
    E_ALL_FAILED,
    E_BAD_ARG,
    E_BAIDU_BLOCKED,
    E_BILI_RISK,
    E_EMPTY_QUERY,
    E_ENGINE_DISABLED,
    E_HTTP,
    E_NETWORK,
    E_NO_RESULT,
    E_PARSE,
    E_TIMEOUT,
    E_UNKNOWN_ENGINE,
    FRESHNESS_VALUES,
    SearchOutcome,
)
from cgbot.search.service import ENGINE_LABELS, MAX_QUERY_CHARS, SearchService

MAX_SNIPPET_CHARS = 200

_CODE_TEXT: dict[str, str] = {
    E_TIMEOUT: "搜索请求超时，请稍后再试。",
    E_NETWORK: "网络异常，搜索请求失败。",
    E_HTTP: "搜索服务返回异常响应。",
    E_PARSE: "搜索结果解析失败。",
    E_NO_RESULT: "未找到相关结果。",
    E_BAIDU_BLOCKED: "百度触发安全验证，暂不可用。",
    E_BILI_RISK: "哔哩哔哩触发风控限流，暂不可用。",
    E_EMPTY_QUERY: "搜索关键词不能为空。",
    E_BAD_ARG: "参数不合法。",
    E_UNKNOWN_ENGINE: "未知的搜索引擎。",
    E_ENGINE_DISABLED: "该搜索引擎未启用。",
}

GeminiFactory = Callable[[SearchGeminiConfig], ToolSpec]


def _error(message: str) -> ToolResult:
    return ToolResult(output=message, is_error=True)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _unknown_args(args: dict[str, Any], allowed: set[str]) -> ToolResult | None:
    unknown = sorted(set(args) - allowed)
    if unknown:
        return _error(f"unknown argument(s): {', '.join(unknown)}")
    return None


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def format_outcome(query: str, outcome: SearchOutcome) -> str:
    """Successful outcome as numbered entries; engine tags shown only when several were queried."""
    show_source = len(outcome.attempted) > 1
    lines = [f"搜索结果（关键词：{query}，共 {len(outcome.results)} 条）："]
    for index, item in enumerate(outcome.results, 1):
        tag = f"[{ENGINE_LABELS.get(item.source, item.source)}] " if show_source else ""
        lines.append(f"{index}. {tag}{item.title}")
        if item.meta:
            lines.append(f"   {item.meta}")
        if item.snippet:
            lines.append(f"   摘要：{_clip(item.snippet, MAX_SNIPPET_CHARS)}")
        lines.append(f"   链接：{item.url}")
    if outcome.engine_codes:
        lines.append("——")
        lines.extend(_engine_failure_lines(outcome.engine_codes))
    return "\n".join(lines)


def _engine_failure_lines(engine_codes: tuple[tuple[str, str], ...]) -> list[str]:
    return [
        f"{ENGINE_LABELS.get(name, name)}：{_CODE_TEXT.get(code, code)}"
        for name, code in engine_codes
    ]


def format_failure(outcome: SearchOutcome) -> str:
    if outcome.code == E_ALL_FAILED:
        details = "；".join(_engine_failure_lines(outcome.engine_codes))
        return f"搜索失败：所有搜索引擎均未返回结果。（{details}）"
    text = _CODE_TEXT.get(outcome.code, "搜索失败。")
    return f"搜索失败（{outcome.code}）：{text}"


_WEB_SEARCH_DESCRIPTION = (
    "联网搜索，返回所选来源的标题、链接与摘要（哔哩哔哩结果为视频，附 UP 主与播放量等）。"
    "何时用：需要事实核对、新闻或近期事件、实时信息，或不确定某词语、梗的出处时调用；"
    "闲聊、常识或你已确定知道的内容不要搜索。结果只是参考：用自己的话概括作答，"
    "需要时附上链接，不要整段照抄摘要。"
)
_GEMINI_HINT = (
    "另有 gemini_search（Google 来源的 AI 搜索）：本工具结果不足，或需要英文、国际、"
    "权威来源时可改用它。"
)


def web_search_tool(service: SearchService, *, with_gemini: bool) -> ToolSpec:
    """The ``web_search`` tool bound to ``service``; schema lists only enabled engines."""
    config = service.config
    engines = list(service.engine_names)
    allowed = {"query", "engine", "count", "freshness"}
    description = _WEB_SEARCH_DESCRIPTION + (f" {_GEMINI_HINT}" if with_gemini else "")

    async def handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        if (refused := _unknown_args(args, allowed)) is not None:
            return refused
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            return _error("query is required")
        if len(query.strip()) > MAX_QUERY_CHARS:
            return _error(f"query must be at most {MAX_QUERY_CHARS} characters")
        engine = args.get("engine")
        if engine is not None and not isinstance(engine, str):
            return _error("engine must be a string")
        count = args.get("count")
        if count is not None and (not _is_int(count) or not 1 <= count <= config.max_count):
            return _error(f"count must be an integer 1-{config.max_count}")
        freshness = args.get("freshness")
        if freshness is not None and freshness not in FRESHNESS_VALUES:
            return _error("freshness must be one of day/week/month/year")

        outcome = await service.search(query, engine=engine, count=count, freshness=freshness)
        if not outcome.ok:
            return _error(format_failure(outcome))
        return ToolResult(output=format_outcome(query.strip(), outcome))

    properties: dict[str, Any] = {
        "query": {
            "type": "string",
            "maxLength": MAX_QUERY_CHARS,
            "description": "搜索关键词，简短具体",
        },
        "count": {
            "type": "integer",
            "minimum": 1,
            "maximum": config.max_count,
            "description": f"返回条数，默认 {config.default_count}",
        },
        "freshness": {
            "type": "string",
            "enum": list(FRESHNESS_VALUES),
            "description": "时间范围，仅必应生效：day/week/month/year",
        },
    }
    if engines:
        properties["engine"] = {
            "type": "string",
            "enum": engines,
            "description": "指定单个搜索引擎；省略时使用默认引擎（未配置则合并全部已启用引擎）",
        }
    return ToolSpec(
        name="web_search",
        description=description,
        parameters_schema={
            "type": "object",
            "properties": properties,
            "required": ["query"],
        },
        handler=handler,
    )


def _default_gemini_factory(config: SearchGeminiConfig) -> ToolSpec:
    # Imported lazily: the Gemini relay client is only needed when [search.gemini] is enabled.
    from cgbot.search.gemini import gemini_search_tool

    if config.token is None:
        raise ValueError("[search.gemini] token_ref is required when enabled")
    return gemini_search_tool(
        config.endpoint,
        config.token.reveal(),
        model=config.model,
        timeout=config.timeout_seconds,
    )


def search_tools(
    service: SearchService,
    *,
    gemini_factory: GeminiFactory | None = None,
) -> list[ToolSpec]:
    """``web_search`` plus ``gemini_search`` when ``[search.gemini]`` is enabled."""
    gemini = service.config.gemini
    specs = [web_search_tool(service, with_gemini=gemini.enabled)]
    if gemini.enabled:
        factory = gemini_factory or _default_gemini_factory
        specs.append(factory(gemini))
    return specs
