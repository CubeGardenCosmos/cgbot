"""MuseScore 社区乐谱搜索与第 1 页预览(私聊只读工具)。

抓取与解析全部在用户沙盒里的 ``cgdl`` CLI(``sandbox/tools/cgdl``)中完成。bot 侧只做:

- 经 Docker exec 在用户沙盒中一次性运行 ``cgdl musescore-search`` / ``cgdl musescore-get``;
- 解析 cgdl 的单行 JSON 输出(成功走 stdout,失败走 stderr 的 ``{"error": {...}}``);
- 预览时从用户工作区读取 cgdl 写出的 PNG,校验后发给发起请求的用户。

``blocked``(Cloudflare 拦截出口 IP)不可重试;进程内负缓存 10 分钟,期间直接返回错误、不再执行。
沙盒或 exec 不可用时返回 ``sandbox_unavailable``。

搜索由 cgdl 带 ``type=non-official`` 发出,只返回社区乐谱。
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

from cgbot.agent.tools import ToolContext, ToolResult, ToolSpec
from cgbot.coding.docker import DockerError, ExecResult, ForbiddenRoute
from cgbot.coding.imagegen import read_workspace_file
from cgbot.coding.sandbox import DockerSandboxProvider, SandboxError
from cgbot.model import OutboundMessage, Segment

log = logging.getLogger(__name__)

BASE_URL = "https://musescore.com"
PAGE_HOSTS: frozenset[str] = frozenset({"musescore.com", "www.musescore.com"})
# 与 cgdl.files.DEFAULT_OUTPUT_DIR 一致;cgdl 返回的 path 为沙盒内绝对路径。
OUTPUT_PREFIX = "/workspace/artifacts/music/"
SANDBOX_WORKSPACE = "/workspace/"

MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_TERM_CHARS = 100
MAX_LIMIT = 10
DEFAULT_LIMIT = 5
REMEMBER_MAX = 500
BLOCKED_TTL_S = 600.0
EXEC_TIMEOUT_S = 180.0
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

BLOCKED_HINT = "出口被 Cloudflare 拦截,不要重试或换镜像;请用户直接发 MIDI/MusicXML 或修复网络出口"

E_BLOCKED = "blocked"
E_BAD_OUTPUT = "bad_output"
E_BAD_REF = "bad_ref"
E_SANDBOX_UNAVAILABLE = "sandbox_unavailable"
E_NO_PREVIEW = "no_preview"

_SCORE_PATH = re.compile(r"/user/(\d{1,123})/scores/(\d{1,123})/?")
_BARE_ID = re.compile(r"\d{1,123}")

_SEARCH_ARGS = frozenset({"query", "title", "composer", "instrument", "limit"})
_PREVIEW_ARGS = frozenset({"score"})


class MuseScoreError(Exception):
    """可向模型解释的失败。``code`` 为稳定错误码(cgdl 的错误码原样透传)。

    ``retryable=False`` 表示同样的调用再试也无意义(当前仅 ``blocked``)。
    """

    def __init__(self, code: str, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class SandboxUnavailable(Exception):
    """用户沙盒或 Docker exec 不可用(容器起不来、代理拒绝、cgdl 不可执行等)。"""


class CgdlRunner(Protocol):
    """在用户沙盒里执行 ``argv``(首项为 ``cgdl``)并返回输出。
    沙盒不可用时抛 SandboxUnavailable。"""

    async def run(self, user_id: str, argv: Sequence[str]) -> ExecResult: ...


class SandboxCgdlRunner:
    """经用户容器的 Docker exec 一次性运行 cgdl(create exec / start / inspect ExitCode)。"""

    def __init__(self, sandbox: DockerSandboxProvider) -> None:
        self._sandbox = sandbox

    async def run(self, user_id: str, argv: Sequence[str]) -> ExecResult:
        try:
            async with self._sandbox.lease(user_id) as endpoint:
                result = await self._sandbox.docker.exec_run(
                    endpoint.container, argv, timeout_s=EXEC_TIMEOUT_S
                )
        except (
            SandboxError,
            DockerError,
            ForbiddenRoute,
            httpx.HTTPError,
            OSError,
            ValueError,
        ) as exc:
            log.warning("musescore sandbox exec failed: %s", type(exc).__name__)
            raise SandboxUnavailable(f"沙盒执行失败: {type(exc).__name__}") from exc
        if result.exit_code in (126, 127):
            raise SandboxUnavailable(f"沙盒内 cgdl 不可执行(exit {result.exit_code})")
        return result


@dataclass(frozen=True, slots=True)
class ScoreHit:
    score_id: int
    url: str
    title: str
    composer: str | None
    instruments: tuple[str, ...]
    pages: int | None
    rating: float | None
    rating_count: int | None
    duration: str | None
    license: str


@dataclass(frozen=True, slots=True)
class ScoreRef:
    score_id: int
    url: str | None


def parse_score_ref(raw: str) -> ScoreRef:
    """完整乐谱 url(https://musescore.com/user/<uid>/scores/<id>)或本进程内搜索过的纯数字 id。"""
    text = raw.strip()
    if _BARE_ID.fullmatch(text):
        return ScoreRef(score_id=int(text), url=None)
    parts = urllib.parse.urlsplit(text)
    if parts.scheme != "https" or (parts.hostname or "") not in PAGE_HOSTS:
        raise MuseScoreError(E_BAD_REF, "只接受 https://musescore.com 的乐谱页面 url 或纯数字 id")
    match = _SCORE_PATH.fullmatch(parts.path)
    if match is None:
        raise MuseScoreError(
            E_BAD_REF, "乐谱 url 格式应为 https://musescore.com/user/<uid>/scores/<id>"
        )
    uid, score_id = match.group(1), int(match.group(2))
    return ScoreRef(score_id=score_id, url=f"{BASE_URL}/user/{uid}/scores/{score_id}")


class MuseScoreClient:
    """cgdl 的薄封装:运行命令、解析 JSON、维护 blocked 负缓存与近期搜索结果。

    一个进程只应有一个实例:Cloudflare 拦截的是出口 IP,负缓存需跨用户共享。
    ``workspace_root`` 为宿主机上的 ``host_data_dir``(用户工作区为 ``<root>/users/<uid>``)。
    """

    def __init__(
        self,
        runner: CgdlRunner,
        workspace_root: Path,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._runner = runner
        self._workspace_root = workspace_root
        self._clock = clock
        self._blocked_until: float | None = None
        self._known: dict[int, str] = {}

    async def search(self, user_id: str, query: str, limit: int) -> list[ScoreHit]:
        data = await self._call(user_id, ["musescore-search", "--limit", str(limit), "--", query])
        results = data.get("results")
        if not isinstance(results, list):
            raise MuseScoreError(E_BAD_OUTPUT, "cgdl 搜索结果缺少 results 列表")
        hits = [_hit_from_dict(item) for item in results]
        for hit in hits:
            self._remember(hit.score_id, hit.url)
        return hits

    def resolve(self, ref: ScoreRef) -> str:
        if ref.url is not None:
            self._remember(ref.score_id, ref.url)
            return ref.url
        url = self._known.get(ref.score_id)
        if url is None:
            raise MuseScoreError(E_BAD_REF, "未知的乐谱 id;请先搜索,或提供完整乐谱 url")
        return url

    async def first_page_png(self, user_id: str, url: str) -> tuple[bytes, str | None]:
        """让 cgdl 渲染第 1 页为 PNG,读取并校验后返回 (图片字节, 乐谱标题)。"""
        data = await self._call(user_id, ["musescore-get", url, "--kind", "png"])
        path = data.get("path")
        if not isinstance(path, str) or not path.startswith(OUTPUT_PREFIX) or ".." in path:
            raise MuseScoreError(E_BAD_OUTPUT, "cgdl 返回的输出路径不在乐谱输出目录内")
        if not path.endswith(".png"):
            raise MuseScoreError(E_BAD_OUTPUT, "cgdl 返回的不是 PNG 文件")
        parts = tuple(path[len(SANDBOX_WORKSPACE) :].split("/"))
        try:
            png = read_workspace_file(self._workspace_root, user_id, parts, MAX_IMAGE_BYTES)
        except (OSError, ValueError) as exc:
            raise MuseScoreError(E_NO_PREVIEW, f"无法读取预览图片: {exc}") from exc
        if not png.startswith(PNG_MAGIC):
            raise MuseScoreError(E_NO_PREVIEW, "预览文件不是 PNG 图片")
        title = data.get("title")
        return png, title if isinstance(title, str) else None

    async def _call(self, user_id: str, args: Sequence[str]) -> dict[str, Any]:
        self._guard_blocked()
        try:
            result = await self._runner.run(user_id, ["cgdl", *args])
        except SandboxUnavailable as exc:
            raise MuseScoreError(E_SANDBOX_UNAVAILABLE, str(exc)) from exc
        if result.exit_code == 0:
            try:
                data = json.loads(result.stdout)
            except ValueError as exc:
                raise MuseScoreError(E_BAD_OUTPUT, "cgdl 标准输出不是 JSON") from exc
            if not isinstance(data, dict):
                raise MuseScoreError(E_BAD_OUTPUT, "cgdl 标准输出不是 JSON 对象")
            return data
        code, message = _parse_cgdl_error(result.stderr)
        if code == E_BLOCKED:
            self._blocked_until = self._clock() + BLOCKED_TTL_S
            raise MuseScoreError(E_BLOCKED, BLOCKED_HINT, retryable=False)
        raise MuseScoreError(code, message)

    def _guard_blocked(self) -> None:
        if self._blocked_until is None:
            return
        if self._clock() < self._blocked_until:
            raise MuseScoreError(E_BLOCKED, BLOCKED_HINT, retryable=False)
        self._blocked_until = None

    def _remember(self, score_id: int, url: str) -> None:
        self._known.pop(score_id, None)
        self._known[score_id] = url
        while len(self._known) > REMEMBER_MAX:
            del self._known[next(iter(self._known))]


def _parse_cgdl_error(stderr: bytes) -> tuple[str, str]:
    try:
        data = json.loads(stderr)
    except ValueError:
        return E_BAD_OUTPUT, "cgdl 错误输出不是 JSON"
    error = data.get("error") if isinstance(data, dict) else None
    if not isinstance(error, dict):
        return E_BAD_OUTPUT, "cgdl 错误输出缺少 error 对象"
    code = error.get("code")
    message = error.get("message")
    if not isinstance(code, str) or not isinstance(message, str):
        return E_BAD_OUTPUT, "cgdl 错误输出缺少 code 或 message"
    return code, message


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _req_str(item: Mapping[str, Any], key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str):
        raise MuseScoreError(E_BAD_OUTPUT, f"cgdl 搜索结果缺少字段 {key}")
    return value


def _opt_str(item: Mapping[str, Any], key: str) -> str | None:
    value = item.get(key)
    if value is None or isinstance(value, str):
        return value
    raise MuseScoreError(E_BAD_OUTPUT, f"cgdl 搜索结果字段 {key} 类型错误")


def _opt_int(item: Mapping[str, Any], key: str) -> int | None:
    value = item.get(key)
    if value is None or _is_int(value):
        return value
    raise MuseScoreError(E_BAD_OUTPUT, f"cgdl 搜索结果字段 {key} 类型错误")


def _opt_number(item: Mapping[str, Any], key: str) -> float | None:
    value = item.get(key)
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    raise MuseScoreError(E_BAD_OUTPUT, f"cgdl 搜索结果字段 {key} 类型错误")


def _hit_from_dict(item: object) -> ScoreHit:
    if not isinstance(item, dict):
        raise MuseScoreError(E_BAD_OUTPUT, "cgdl 搜索结果条目不是对象")
    score_id = item.get("score_id")
    if not isinstance(score_id, int) or isinstance(score_id, bool):
        raise MuseScoreError(E_BAD_OUTPUT, "cgdl 搜索结果字段 score_id 类型错误")
    instruments = item.get("instruments", [])
    if not isinstance(instruments, list) or not all(isinstance(x, str) for x in instruments):
        raise MuseScoreError(E_BAD_OUTPUT, "cgdl 搜索结果字段 instruments 类型错误")
    return ScoreHit(
        score_id=score_id,
        url=_req_str(item, "url"),
        title=_req_str(item, "title"),
        composer=_opt_str(item, "composer"),
        instruments=tuple(instruments),
        pages=_opt_int(item, "pages"),
        rating=_opt_number(item, "rating"),
        rating_count=_opt_int(item, "rating_count"),
        duration=_opt_str(item, "duration"),
        license=_req_str(item, "license"),
    )


def _error(message: str) -> ToolResult:
    return ToolResult(output=message, is_error=True)


def _failure(action: str, exc: MuseScoreError) -> ToolResult:
    retry = "true" if exc.retryable else "false"
    return _error(f"Musescore {action}失败({exc.code}, retryable={retry}):{exc.message}")


def _optional_term(args: dict[str, Any], name: str) -> str | None | ToolResult:
    value = args.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        return _error(f"{name} must be a string")
    text = " ".join(value.split())
    if len(text) > MAX_TERM_CHARS:
        return _error(f"{name} must be at most {MAX_TERM_CHARS} characters")
    return text or None


def format_hits(hits: Sequence[ScoreHit], *, instrument: str | None) -> str:
    if not hits:
        return "没有找到符合条件的社区乐谱(已排除官方乐谱)。"
    lines = [f"找到 {len(hits)} 条社区乐谱(已排除官方乐谱,按相关度排序):"]
    for index, hit in enumerate(hits, 1):
        instruments = "、".join(hit.instruments) if hit.instruments else "未注明"
        pages = f"{hit.pages} 页" if hit.pages else "页数未知"
        rating = (
            f"评分 {hit.rating:.2f}({hit.rating_count or 0} 人)"
            if hit.rating is not None
            else "无评分"
        )
        lines.append(
            f"{index}. {hit.title} | 作曲 {hit.composer or '未注明'} | "
            f"乐器 {instruments} | {pages} | "
            f"{rating} | 时长 {hit.duration or '未知'} | 许可 {hit.license}"
        )
        lines.append(f"   乐谱 id {hit.score_id} | {hit.url}")
    if instrument:
        lines.append(f"乐器「{instrument}」已作为关键词加入搜索。")
    return "\n".join(lines)


_SEARCH_DESCRIPTION = (
    "搜索 MuseScore 社区乐谱(只搜索,不下载;从不返回官方乐谱)。query 为模糊关键词,可拼错或缺词;"
    "title、composer 可选;instrument 可用中英文乐器名(钢琴/piano、小提琴/violin、吉他/guitar 等)。"
    "返回乐谱 id 与完整 url。需要渲染或下载时,先用本工具选定乐谱,"
    "再在交办任务的提示中写入该乐谱的完整 url,"
    "由沙盒执行 cgdl musescore-get。"
)
_PREVIEW_DESCRIPTION = (
    "把某个乐谱的第 1 页图片直接发给当前用户(只发图,不下载乐谱)。score 填搜索结果中的完整 url;"
    "若本进程内刚搜索过,也可填纯数字 id。"
)


def musescore_search_tool(client: MuseScoreClient) -> ToolSpec:
    async def handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        unknown = sorted(set(args) - _SEARCH_ARGS)
        if unknown:
            return _error(f"unknown argument(s): {', '.join(unknown)}")
        fields: dict[str, str | None] = {}
        for name in ("query", "title", "composer", "instrument"):
            value = _optional_term(args, name)
            if isinstance(value, ToolResult):
                return value
            fields[name] = value
        terms = [t for t in (fields["query"], fields["title"], fields["composer"]) if t]
        if not terms:
            return _error("query、title、composer 至少提供一个")
        limit = args.get("limit", DEFAULT_LIMIT)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
            return _error(f"limit must be an integer 1-{MAX_LIMIT}")
        # cgdl 的 musescore-search 只接受一个关键词串;乐器以关键词形式并入。
        instrument = fields["instrument"]
        words = terms + ([instrument] if instrument else [])
        try:
            hits = await client.search(ctx.user_id, " ".join(words), limit)
        except MuseScoreError as exc:
            return _failure("搜索", exc)
        return ToolResult(output=format_hits(hits, instrument=instrument))

    return ToolSpec(
        name="musescore_search",
        description=_SEARCH_DESCRIPTION,
        parameters_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "maxLength": MAX_TERM_CHARS,
                    "description": "模糊关键词",
                },
                "title": {
                    "type": "string",
                    "maxLength": MAX_TERM_CHARS,
                    "description": "曲名(可选)",
                },
                "composer": {
                    "type": "string",
                    "maxLength": MAX_TERM_CHARS,
                    "description": "作曲者(可选)",
                },
                "instrument": {
                    "type": "string",
                    "maxLength": MAX_TERM_CHARS,
                    "description": "乐器,中英文均可,如 钢琴、小提琴、piano、violin",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_LIMIT,
                    "description": f"返回条数,默认 {DEFAULT_LIMIT}",
                },
            },
        },
        handler=handler,
    )


def musescore_preview_tool(client: MuseScoreClient) -> ToolSpec:
    async def handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        unknown = sorted(set(args) - _PREVIEW_ARGS)
        if unknown:
            return _error(f"unknown argument(s): {', '.join(unknown)}")
        score = args.get("score")
        if not isinstance(score, str) or not score.strip() or len(score) > 200:
            return _error("score must be a non-empty url or score id (at most 200 characters)")
        try:
            ref = parse_score_ref(score)
            url = client.resolve(ref)
            png, title = await client.first_page_png(ctx.user_id, url)
        except MuseScoreError as exc:
            return _failure("预览", exc)
        encoded = base64.b64encode(png).decode("ascii")
        result = await ctx.sender(
            OutboundMessage(
                scope="private",
                target_id=ctx.source_channel.account,
                segments=(Segment("image", {"file": f"base64://{encoded}"}),),
                reply_to=None,
            )
        )
        if not result.ok:
            return _error(f"send failed: {result.error or 'unknown error'}")
        label = f"《{title}》" if title else f"id {ref.score_id}"
        return ToolResult(output=f"已把乐谱 {label} 的第 1 页图片发给用户。")

    return ToolSpec(
        name="musescore_preview",
        description=_PREVIEW_DESCRIPTION,
        parameters_schema={
            "type": "object",
            "properties": {
                "score": {
                    "type": "string",
                    "maxLength": 200,
                    "description": "搜索结果中的完整乐谱 url,或本进程内搜索过的纯数字 id",
                },
            },
            "required": ["score"],
        },
        handler=handler,
    )


def musescore_tools(client: MuseScoreClient) -> list[ToolSpec]:
    return [musescore_search_tool(client), musescore_preview_tool(client)]
