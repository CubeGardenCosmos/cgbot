"""MuseScore 社区乐谱搜索与第 1 页预览(私聊只读工具)。

Cloudflare 策略与页面解析移植自 ``sandbox/tools/cgdl``(同一作者的沙盒 CLI):

- ``cgdl.http`` 策略 1:带真实浏览器请求头的普通 HTTP,请求之间至少间隔 1 秒;
- ``cgdl.parse``:挑战页识别(``cf-mitigated`` 响应头或挑战标题),搜索页数据区 JSON 解析;
- ``cgdl.urls``:主机白名单。

cgdl 的浏览器兜底通道(Playwright + Chromium)没有移植:bot 镜像不含 Chromium。遇到挑战时
本模块返回 ``blocked`` 错误,不静默降级,也不把挑战页当作结果。

搜索永远带 ``type=non-official``,并在代码里再过滤一次官方标记,确保不返回官方乐谱。
"""

from __future__ import annotations

import asyncio
import base64
import html as html_lib
import json
import re
import time
import unicodedata
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import urlsplit

import httpx

from cgbot.agent.tools import ToolContext, ToolResult, ToolSpec
from cgbot.model import OutboundMessage, Segment

BASE_URL = "https://musescore.com"
SEARCH_URL = f"{BASE_URL}/sheetmusic"
PAGE_HOSTS: frozenset[str] = frozenset({"musescore.com", "www.musescore.com"})
IMAGE_HOSTS: frozenset[str] = frozenset({"cdn.ustatik.com"})

# cgdl.http:MIN_INTERVAL_S / TIMEOUT_S / BROWSER_HEADERS(策略 1 的请求头)。
MIN_INTERVAL_S = 1.0
TIMEOUT_S = 30.0
BROWSER_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "sec-ch-ua": '"Chromium";v="140", "Not=A?Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Linux"',
}

MAX_HTML_BYTES = 4 * 1024 * 1024
MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_REQUESTS_PER_SEARCH = 2
MAX_TERM_CHARS = 100
MAX_LIMIT = 10
DEFAULT_LIMIT = 5
MIN_RELEVANCE = 0.5
TOKEN_MIN_RATIO = 0.75
REMEMBER_MAX = 500
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

E_BLOCKED = "blocked"
E_NOT_FOUND = "not_found"
E_HTTP = "http_error"
E_NETWORK = "network_error"
E_PARSE = "parse_failed"
E_TOO_LARGE = "too_large"
E_BAD_REF = "bad_ref"
E_NO_PREVIEW = "no_preview"
E_REFUSED = "refused_host"

# cgdl.parse 的挑战标题文案。
_CHALLENGE_TITLES = ("Just a moment", "Attention Required")
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_SCORE_PATH = re.compile(r"/user/(\d{1,12})/scores/(\d{1,12})/?")
_BARE_ID = re.compile(r"[0-9]{1,12}")
# 搜索页数据区:属性值是 HTML 转义的 JSON,含 "store" 键。属性名是随机哈希,故不按名字取。
_JSON_ATTR_RE = re.compile(r'\s[\w:-]+="([^"]*&quot;store&quot;[^"]*)"')
_BRACKET_TAG_RE = re.compile(r"\[/?b\]", re.IGNORECASE)
_OG_URL_RE = re.compile(r'<meta property="og:url" content="([^"]+)"')
_LARGE_PNG_RE = re.compile(
    r"https://cdn\.ustatik\.com/musescore/scoredata/g/[0-9a-f]+/score_0\.png@500x660\?[^\"'\s<>]*"
)
_ORIGINAL_PNG_RE = re.compile(
    r"https://cdn\.ustatik\.com/musescore/scoredata/g/[0-9a-f]+/score_0\.png\?[^\"'\s<>]*"
)

# 乐器别名 -> musescore 乐器筛选 id(由搜索页 filters.instrument 实测取得)。
# 未列入的乐器名不猜测 id,而是作为关键词加入搜索文本。
_INSTRUMENT_IDS: dict[str, int] = {
    "piano": 2,
    "钢琴": 2,
    "pianoforte": 2,
    "organ": 12,
    "管风琴": 12,
    "harpsichord": 11,
    "大键琴": 11,
    "accordion": 10,
    "手风琴": 10,
    "voice": 5,
    "vocal": 5,
    "vocals": 5,
    "人声": 5,
    "声乐": 5,
    "flute": 47,
    "长笛": 47,
    "violin": 68,
    "小提琴": 68,
    "viola": 69,
    "中提琴": 69,
    "cello": 70,
    "violoncello": 70,
    "大提琴": 70,
    "guitar": 72,
    "吉他": 72,
    "吉它": 72,
    "木吉他": 72,
    "bass guitar": 73,
    "电贝斯": 73,
    "贝斯": 73,
    "harp": 80,
    "竖琴": 80,
}


class MuseScoreError(Exception):
    """可向模型解释的失败;``code`` 为稳定错误码。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


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


# ---------------------------------------------------------------------------
# 文本规范化与模糊排序(纯函数)
# ---------------------------------------------------------------------------


def normalise(text: str) -> str:
    """小写、去重音、去标点,并在字母与数字之间插入空格("op9" -> "op 9")。"""
    decomposed = unicodedata.normalize("NFKD", text)
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    spaced = re.sub(r"(?<=[^\W\d_])(?=\d)|(?<=\d)(?=[^\W\d_])", " ", plain.casefold())
    return " ".join(re.findall(r"[^\W_]+", spaced))


def search_variants(terms: Sequence[str]) -> list[str]:
    """至多 ``MAX_REQUESTS_PER_SEARCH`` 个搜索文本:原文(合并各词)与规范化形式。"""
    joined = " ".join(" ".join(term.split()) for term in terms if term.strip())
    variants = [joined]
    alternate = normalise(joined)
    if alternate and alternate != joined.casefold():
        variants.append(alternate)
    return variants[:MAX_REQUESTS_PER_SEARCH]


def _relevance(query_tokens: list[str], target: str, target_tokens: list[str]) -> float:
    """每个查询词取其在目标中的最佳相似度(子串命中为 1),再取平均。"""
    if not query_tokens:
        return 0.0
    total = 0.0
    for token in query_tokens:
        if token in target:
            total += 1.0
            continue
        best = max(
            (SequenceMatcher(None, token, candidate).ratio() for candidate in target_tokens),
            default=0.0,
        )
        total += best if best >= TOKEN_MIN_RATIO else 0.0
    return total / len(query_tokens)


def rank_hits(hits: Sequence[ScoreHit], terms: Sequence[str], limit: int) -> list[ScoreHit]:
    """按与 title+composer 的相似度排序;低于 ``MIN_RELEVANCE`` 的结果丢弃。"""
    query = normalise(" ".join(terms))
    query_tokens = query.split()
    if not query_tokens:
        return list(hits[:limit])
    scored: list[tuple[float, float, ScoreHit]] = []
    for hit in hits:
        target = normalise(" ".join(part for part in (hit.title, hit.composer or "") if part))
        score = _relevance(query_tokens, target, target.split())
        if score < MIN_RELEVANCE:
            continue
        whole = SequenceMatcher(None, query, target).ratio()
        scored.append((score, whole, hit))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [hit for _, _, hit in scored[:limit]]


def resolve_instrument(text: str) -> tuple[int | None, str | None]:
    """返回 (筛选 id, 未识别时的原文关键词)。"""
    key = normalise(text)
    if not key:
        return None, None
    for alias, instrument_id in _INSTRUMENT_IDS.items():
        if normalise(alias) == key:
            return instrument_id, None
    return None, text.strip()


# ---------------------------------------------------------------------------
# 页面解析(纯函数)
# ---------------------------------------------------------------------------


def is_challenge_html(body: bytes) -> bool:
    """cgdl.parse.is_challenge 的标题判断。"""
    match = _TITLE_RE.search(body[:65536].decode("utf-8", errors="replace"))
    title = html_lib.unescape(match[1]).strip() if match else ""
    return any(title.startswith(prefix) for prefix in _CHALLENGE_TITLES)


def _as_str(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _as_number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _clean_title(title: str) -> str:
    return " ".join(html_lib.unescape(_BRACKET_TAG_RE.sub("", title)).split())


def is_official(node: Mapping[str, Any]) -> bool:
    """任何官方/出版社标记即视为官方乐谱(防御性检查;搜索 URL 已带 type=non-official)。"""
    if any(node.get(key) for key in ("is_official", "isOfficial", "official")):
        return True
    if node.get("publisher"):
        return True
    user = node.get("user")
    if isinstance(user, dict) and user.get("is_publisher"):
        return True
    badges = node.get("badges") or node.get("labels") or []
    if isinstance(badges, list) and any("official" in str(badge).lower() for badge in badges):
        return True
    return False


def _hit_of(node: Mapping[str, Any]) -> ScoreHit | None:
    href = _as_str(node.get("href"))
    score_id = _as_int(node.get("id"))
    title = _as_str(node.get("title"))
    if href is None or score_id is None or title is None:
        return None
    parts = urlsplit(href)
    if parts.hostname not in PAGE_HOSTS:
        return None
    match = _SCORE_PATH.fullmatch(parts.path)
    if match is None or int(match[2]) != score_id:
        return None
    instruments_raw = node.get("instruments")
    instruments: tuple[str, ...] = ()
    if isinstance(instruments_raw, list):
        instruments = tuple(
            name
            for entry in instruments_raw
            if isinstance(entry, dict) and (name := _as_str(entry.get("name")))
        )
    rating = node.get("rating")
    rating_value = _as_number(rating.get("rating")) if isinstance(rating, dict) else None
    rating_count = _as_int(rating.get("count")) if isinstance(rating, dict) else None
    license_text = _as_str(node.get("license"))
    if license_text is None:
        license_text = "公有领域" if node.get("is_public_domain") else "未注明"
    return ScoreHit(
        score_id=score_id,
        url=f"{BASE_URL}/user/{int(match[1])}/scores/{score_id}",
        title=_clean_title(title),
        composer=_as_str(node.get("composer_name")),
        instruments=instruments,
        pages=_as_int(node.get("pages_count")),
        rating=rating_value,
        rating_count=rating_count,
        duration=_as_str(node.get("duration")),
        license=license_text,
    )


def _search_scores(html: str) -> list[dict[str, Any]]:
    for match in _JSON_ATTR_RE.finditer(html):
        try:
            payload = json.loads(html_lib.unescape(match[1]))
        except ValueError:
            continue
        store = payload.get("store") if isinstance(payload, dict) else None
        page = store.get("page") if isinstance(store, dict) else None
        data = page.get("data") if isinstance(page, dict) else None
        scores = data.get("scores") if isinstance(data, dict) else None
        if isinstance(scores, list):
            return [node for node in scores if isinstance(node, dict)]
    raise MuseScoreError(E_PARSE, "search page has no result data")


def parse_search_html(html: str) -> tuple[list[ScoreHit], int]:
    """社区乐谱列表与被过滤的官方条数。无结果时返回空列表。"""
    hits: list[ScoreHit] = []
    dropped = 0
    seen: set[int] = set()
    for node in _search_scores(html):
        if is_official(node):
            dropped += 1
            continue
        hit = _hit_of(node)
        if hit is None or hit.score_id in seen:
            continue
        seen.add(hit.score_id)
        hits.append(hit)
    return hits, dropped


def page_score_id(html: str) -> int:
    match = _OG_URL_RE.search(html)
    if match is None:
        raise MuseScoreError(E_PARSE, "score page has no og:url")
    path = _SCORE_PATH.fullmatch(urlsplit(html_lib.unescape(match[1])).path)
    if path is None:
        raise MuseScoreError(E_PARSE, "score page og:url is not a score path")
    return int(path[2])


def find_first_page_png(html: str) -> str | None:
    """乐谱页内第 1 页 PNG(优先 500x660 缩略图,其次原图)。JSON 数据区里的 ``\\/`` 先还原。"""
    text = html_lib.unescape(html).replace("\\/", "/")
    for pattern in (_LARGE_PNG_RE, _ORIGINAL_PNG_RE):
        match = pattern.search(text)
        if match is not None:
            return match[0]
    return None


def parse_score_ref(value: str) -> ScoreRef:
    """接受纯数字 id 或 ``https://musescore.com/user/<uid>/scores/<id>``。"""
    text = value.strip()
    if _BARE_ID.fullmatch(text):
        return ScoreRef(int(text), None)
    parts = urlsplit(text)
    if parts.scheme != "https" or parts.hostname not in PAGE_HOSTS:
        raise MuseScoreError(E_BAD_REF, "score must be a musescore.com score url or a score id")
    match = _SCORE_PATH.fullmatch(parts.path)
    if match is None:
        raise MuseScoreError(E_BAD_REF, "score url must look like /user/<uid>/scores/<id>")
    score_id = int(match[2])
    return ScoreRef(score_id, f"{BASE_URL}/user/{int(match[1])}/scores/{score_id}")


# ---------------------------------------------------------------------------
# 网络客户端
# ---------------------------------------------------------------------------


class _Throttle:
    """相邻两次网络请求之间至少间隔 ``interval`` 秒(cgdl.http.Throttle 的异步版)。"""

    def __init__(self, interval: float) -> None:
        self._interval = interval
        self._last: float | None = None
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            if self._last is not None:
                remaining = self._interval - (time.monotonic() - self._last)
                if remaining > 0:
                    await asyncio.sleep(remaining)
            self._last = time.monotonic()


def _allowlist_hook(hosts: frozenset[str]) -> Callable[[httpx.Request], Awaitable[None]]:
    """httpx 请求钩子:首次请求与每一跳重定向都必须落在白名单内,发出前拒绝。"""

    async def hook(request: httpx.Request) -> None:
        if request.url.host not in hosts:
            host = request.url.host
            raise MuseScoreError(E_REFUSED, f"refusing host outside the allowlist: {host}")

    return hook


class MuseScoreClient:
    """搜索与第 1 页预览。每次调用新建 httpx 客户端;限速状态在实例内共享。

    ``transport`` 仅供测试注入假传输层。
    """

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        interval: float = MIN_INTERVAL_S,
    ) -> None:
        self._transport = transport
        self._throttle = _Throttle(interval)
        self._known: dict[int, tuple[str, str]] = {}

    async def _get(
        self,
        url: str,
        *,
        hosts: frozenset[str],
        max_bytes: int,
        params: Mapping[str, str] | None = None,
    ) -> bytes:
        await self._throttle.wait()
        body = bytearray()
        try:
            async with httpx.AsyncClient(
                headers=BROWSER_HEADERS,
                timeout=TIMEOUT_S,
                follow_redirects=True,
                event_hooks={"request": [_allowlist_hook(hosts)]},
                transport=self._transport,
            ) as http:
                async with http.stream("GET", url, params=params) as response:
                    status = response.status_code
                    cf_mitigated = response.headers.get("cf-mitigated")
                    content_type = response.headers.get("content-type", "")
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > max_bytes:
                            raise MuseScoreError(E_TOO_LARGE, "response exceeds the size cap")
        except httpx.HTTPError as exc:
            raise MuseScoreError(E_NETWORK, f"request failed ({type(exc).__name__})") from exc
        if status == 404:
            raise MuseScoreError(E_NOT_FOUND, "MuseScore offers no page for this request")
        if (
            status in (403, 429, 503)
            or cf_mitigated == "challenge"
            or ("text/html" in content_type and is_challenge_html(bytes(body)))
        ):
            raise MuseScoreError(
                E_BLOCKED, "Cloudflare 挑战;bot 镜像无浏览器兜底,本次搜索/预览不可用"
            )
        if status != 200:
            raise MuseScoreError(E_HTTP, f"HTTP {status}")
        return bytes(body)

    async def search(
        self,
        text_terms: Sequence[str],
        rank_terms: Sequence[str],
        *,
        instrument_id: int | None,
        limit: int,
    ) -> list[ScoreHit]:
        collected: dict[int, ScoreHit] = {}
        for variant in search_variants(text_terms):
            params = {"text": variant, "type": "non-official"}
            if instrument_id is not None:
                params["instrument"] = str(instrument_id)
            raw = await self._get(
                SEARCH_URL, hosts=PAGE_HOSTS, max_bytes=MAX_HTML_BYTES, params=params
            )
            hits, _dropped = parse_search_html(raw.decode("utf-8", errors="replace"))
            for hit in hits:
                collected.setdefault(hit.score_id, hit)
        for hit in collected.values():
            self._remember(hit.score_id, hit.url, hit.title)
        return rank_hits(list(collected.values()), rank_terms, limit)

    def _remember(self, score_id: int, url: str, title: str) -> None:
        self._known.pop(score_id, None)
        self._known[score_id] = (url, title)
        while len(self._known) > REMEMBER_MAX:
            self._known.pop(next(iter(self._known)))

    def resolve(self, ref: ScoreRef) -> tuple[str, str]:
        """返回 (完整乐谱 url, 标题);纯 id 只接受本进程内搜索过的结果。"""
        if ref.url is not None:
            title = self._known.get(ref.score_id, ("", ""))[1]
            return ref.url, title
        known = self._known.get(ref.score_id)
        if known is None:
            raise MuseScoreError(
                E_BAD_REF, "unknown score id; pass the full url from a search result"
            )
        return known

    async def first_page_png(self, url: str, score_id: int) -> bytes:
        html = (await self._get(url, hosts=PAGE_HOSTS, max_bytes=MAX_HTML_BYTES)).decode(
            "utf-8", errors="replace"
        )
        if page_score_id(html) != score_id:
            raise MuseScoreError(E_NOT_FOUND, "score page does not match the requested id")
        png_url = find_first_page_png(html)
        if png_url is None:
            raise MuseScoreError(E_NO_PREVIEW, "score page has no first-page image")
        data = await self._get(png_url, hosts=IMAGE_HOSTS, max_bytes=MAX_IMAGE_BYTES)
        if not data.startswith(PNG_MAGIC):
            raise MuseScoreError(E_NO_PREVIEW, "first-page image is not a PNG")
        return data


# ---------------------------------------------------------------------------
# 模型工具
# ---------------------------------------------------------------------------

_SEARCH_ARGS = frozenset({"query", "title", "composer", "instrument", "limit"})
_PREVIEW_ARGS = frozenset({"score"})

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


def _error(message: str) -> ToolResult:
    return ToolResult(output=message, is_error=True)


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


def format_hits(hits: Sequence[ScoreHit], *, unmapped_instrument: str | None) -> str:
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
    if unmapped_instrument:
        lines.append(f"未识别的乐器「{unmapped_instrument}」已作为关键词加入搜索。")
    return "\n".join(lines)


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
        instrument_text = fields["instrument"]
        instrument_id, unmapped = (
            resolve_instrument(instrument_text) if instrument_text else (None, None)
        )
        text_terms = terms + ([unmapped] if unmapped else [])
        try:
            hits = await client.search(text_terms, terms, instrument_id=instrument_id, limit=limit)
        except MuseScoreError as exc:
            return _error(f"Musescore 搜索失败({exc.code}):{exc.message}")
        return ToolResult(output=format_hits(hits, unmapped_instrument=unmapped))

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
            url, title = client.resolve(ref)
            png = await client.first_page_png(url, ref.score_id)
        except MuseScoreError as exc:
            return _error(f"Musescore 预览失败({exc.code}):{exc.message}")
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
