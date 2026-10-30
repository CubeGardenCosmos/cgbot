from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from cgbot.agent.tools import ToolContext, ToolResult, ToolSpec

log = logging.getLogger(__name__)

TOOL_NAME = "gemini_search"
RECENCY_VALUES = ("day", "week", "month", "year")
MAX_QUERY_CHARS = 300
RESULT_LIMIT = 5
MAX_ANSWER_CHARS = 1500
MAX_SOURCES_SHOWN = 5
DEFAULT_TIMEOUT_SECONDS = 90.0

_MODEL_RE = re.compile(r"[a-z0-9.\-]{1,64}")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

ERROR_MESSAGES: dict[str, str] = {
    "E_REFUSED": "该问题超出可回答范围(安全策略拒绝)。请礼貌说明无法提供帮助,不要改写问题后重试。",
    "E_NO_RESULT": "没有找到可用的回答。可改用 web_search 再试一次。",
    "E_BAD_QUERY": "查询内容无效(需为 1-300 个字符,不含换行)。",
    "E_BAD_REQUEST": "搜索请求参数无效。",
    "E_AUTH": "搜索服务鉴权失败(配置问题),本次无法使用。",
    "E_BUSY": "搜索服务繁忙,请稍后再试。",
    "E_TIMEOUT": "Google AI 搜索超时,请稍后再试或改用 web_search。",
    "E_NETWORK": "无法连接 Google AI 搜索服务。",
    "E_OMP": "Google AI 搜索执行失败。",
    "E_PARSE": "Google AI 搜索返回的内容无法解析。",
    "E_HTTP": "Google AI 搜索服务返回异常。",
    "E_NOT_FOUND": "搜索服务端点不存在(配置问题)。",
    "E_METHOD": "搜索服务请求方法错误(配置问题)。",
    "E_TOO_LARGE": "搜索请求过大。",
    "E_INTERNAL": "Google AI 搜索服务内部错误。",
}
KNOWN_CODES = frozenset(ERROR_MESSAGES)

HttpFactory = Callable[[], httpx.AsyncClient]


def default_http_factory() -> httpx.AsyncClient:
    return httpx.AsyncClient(follow_redirects=False)


@dataclass(frozen=True, slots=True)
class GeminiSource:
    title: str
    url: str


@dataclass(frozen=True, slots=True)
class GeminiAnswer:
    """``ok`` 为真时有 ``answer``;否则 ``code`` 为稳定错误码(见 :data:`ERROR_MESSAGES`)。"""

    ok: bool
    answer: str = ""
    sources: tuple[GeminiSource, ...] = ()
    code: str | None = None


def _fail(code: str) -> GeminiAnswer:
    return GeminiAnswer(ok=False, code=code)


def _valid_query(query: object) -> str | None:
    if not isinstance(query, str):
        return None
    text = query.strip()
    if not 1 <= len(text) <= MAX_QUERY_CHARS or _CONTROL_RE.search(text):
        return None
    return text


def _parse_sources(raw: object) -> tuple[GeminiSource, ...] | None:
    if not isinstance(raw, list):
        return None
    out: list[GeminiSource] = []
    for item in raw:
        if not isinstance(item, dict):
            return None
        title, url = item.get("title"), item.get("url", "")
        if not isinstance(title, str) or not isinstance(url, str):
            return None
        out.append(GeminiSource(title=title, url=url))
    return tuple(out)


class GeminiSearchClient:
    """调用宿主机 relay 的 ``POST /search``。每次调用新建一个 httpx 客户端并关闭。"""

    def __init__(
        self,
        endpoint: str,
        token: str,
        model: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        *,
        http_factory: HttpFactory = default_http_factory,
    ) -> None:
        parts = urlsplit(endpoint)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise ValueError("gemini search endpoint must be an http(s) URL")
        if not token.strip():
            raise ValueError("gemini search token must not be empty")
        if not _MODEL_RE.fullmatch(model):
            raise ValueError("gemini search model id is invalid")
        if timeout <= 0:
            raise ValueError("gemini search timeout must be positive")
        self._endpoint = endpoint
        self._token = token.strip()
        self._model = model
        self._timeout = timeout
        self._http_factory = http_factory

    @property
    def model(self) -> str:
        return self._model

    async def search(self, query: str, recency: str | None = None) -> GeminiAnswer:
        text = _valid_query(query)
        if text is None:
            return _fail("E_BAD_QUERY")
        if recency is not None and recency not in RECENCY_VALUES:
            return _fail("E_BAD_REQUEST")
        body: dict[str, Any] = {"query": text, "limit": RESULT_LIMIT, "model": self._model}
        if recency is not None:
            body["recency"] = recency
        headers = {"Authorization": f"Bearer {self._token}"}
        try:
            async with self._http_factory() as client:
                response = await client.post(
                    self._endpoint, json=body, headers=headers, timeout=self._timeout
                )
        except httpx.TimeoutException:
            return self._log_failure(_fail("E_TIMEOUT"), status=None)
        except httpx.HTTPError:
            return self._log_failure(_fail("E_NETWORK"), status=None)
        result = _interpret(response)
        if not result.ok:
            self._log_failure(result, status=response.status_code)
        return result

    @staticmethod
    def _log_failure(result: GeminiAnswer, *, status: int | None) -> GeminiAnswer:
        log.warning("gemini_search failed code=%s status=%s", result.code, status)
        return result


def _interpret(response: httpx.Response) -> GeminiAnswer:
    """把 relay 的响应映射为 :class:`GeminiAnswer`。未知形态一律 E_HTTP / E_PARSE,不透传原文。"""
    if response.status_code == 401:
        return _fail("E_AUTH")
    try:
        data: object = response.json()
    except ValueError:
        return _fail("E_HTTP" if response.status_code != 200 else "E_PARSE")
    if not isinstance(data, dict):
        return _fail("E_HTTP" if response.status_code != 200 else "E_PARSE")
    if data.get("ok") is True:
        answer = data.get("answer")
        sources = _parse_sources(data.get("sources", []))
        if not isinstance(answer, str) or sources is None:
            return _fail("E_PARSE")
        if not answer.strip():
            return _fail("E_NO_RESULT")
        return GeminiAnswer(ok=True, answer=answer.strip(), sources=sources)
    code = data.get("code")
    if data.get("ok") is False and isinstance(code, str) and code in KNOWN_CODES:
        return _fail(code)
    return _fail("E_HTTP")


def format_answer(result: GeminiAnswer) -> str:
    answer = result.answer
    if len(answer) > MAX_ANSWER_CHARS:
        answer = answer[:MAX_ANSWER_CHARS] + "…"
    lines = ["Google 来源的 AI 搜索结果。", "回答:", answer]
    if result.sources:
        lines.append("来源:")
        for index, source in enumerate(result.sources[:MAX_SOURCES_SHOWN], start=1):
            label = source.title or source.url or "未命名来源"
            url = f" {source.url}" if source.url else ""
            lines.append(f"{index}. {label}{url}")
    return "\n".join(lines)


_DESCRIPTION = (
    "Google 来源的 AI 搜索:基于 Google 搜索 grounding 的模型回答,附来源链接。"
    "与 web_search(Bing、百度、B 站)并列为网页搜索的四类来源之一。"
    "当其他引擎结果不足,或需要权威、英文、国际来源时使用。较慢,不要连续重复调用。"
)


def gemini_search_tool(
    endpoint: str,
    token: str,
    *,
    model: str,
    timeout: float,
    http_factory: HttpFactory = default_http_factory,
    client: GeminiSearchClient | None = None,
) -> ToolSpec:
    """``gemini_search`` 工具。``endpoint`` 为 relay 的完整 URL,如 ``http://host.docker.internal:8791/search``。"""
    bound_client = client
    if bound_client is None:
        bound_client = GeminiSearchClient(
            endpoint, token, model, timeout, http_factory=http_factory
        )
    allowed = {"query", "recency"}

    async def handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        unknown = sorted(set(args) - allowed)
        if unknown:
            return ToolResult(output=f"unknown argument(s): {', '.join(unknown)}", is_error=True)
        result = await bound_client.search(args.get("query", ""), args.get("recency"))
        if result.ok:
            return ToolResult(output=format_answer(result), is_error=False)
        code = result.code or "E_HTTP"
        return ToolResult(output=f"{code}: {ERROR_MESSAGES[code]}", is_error=True)

    return ToolSpec(
        name=TOOL_NAME,
        description=_DESCRIPTION,
        parameters_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": MAX_QUERY_CHARS},
                "recency": {"type": "string", "enum": list(RECENCY_VALUES)},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        handler=handler,
    )
