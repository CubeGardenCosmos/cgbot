"""HTML 与 JSON 解析(纯函数,无网络):Cloudflare 挑战识别、搜索结果、乐谱页元数据与第 1 页预览图。

- 搜索结果:数据区属性内的 JSON(按值含 ``&quot;store&quot;`` 识别),官方乐谱在此丢弃。
  数据区缺失时退回卡片锚点(``href=.../user/<uid>/scores/<id>``),锚点无官方标记。
  两者都没有则报 PARSE_FAILED。
- 乐谱页元数据:页面内嵌的 `.js-store[data-content]` JSON -> schema.org JSON-LD -> meta/og 标签。
- 预览:乐谱页 og:url 的 id 校验,以及第 1 页 PNG 链接查找(优先 500x660 缩略图)。
"""

from __future__ import annotations

import html as html_lib
import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit

from cgdl.errors import PARSE_FAILED, CgdlError
from cgdl.urls import ScoreRef

_CHALLENGE_TITLES = ("Just a moment", "Attention Required")
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_SCORE_PATH = re.compile(r"/user/(\d{1,12})/scores/(\d{1,12})/?")
_SCORE_HOSTS = frozenset({None, "musescore.com", "www.musescore.com"})
_SEARCH_TITLE_PREFIX = "Search result for"
_NOT_FOUND_TITLE_PREFIX = "Page not found"
# 搜索页数据区:属性值是 HTML 转义的 JSON,含 "store" 键。属性名是随机哈希,故不按名字取。
_JSON_ATTR_RE = re.compile(r'\s[\w:-]+="([^"]*&quot;store&quot;[^"]*)"')
_BRACKET_TAG_RE = re.compile(r"\[/?b\]", re.IGNORECASE)
_LARGE_PNG_RE = re.compile(
    r"https://cdn\.ustatik\.com/musescore/scoredata/g/[0-9a-f]+/score_0\.png@500x660\?[^\"'\s<>]*"
)
_ORIGINAL_PNG_RE = re.compile(
    r"https://cdn\.ustatik\.com/musescore/scoredata/g/[0-9a-f]+/score_0\.png\?[^\"'\s<>]*"
)


@dataclass(frozen=True, slots=True)
class ScoreHit:
    """搜索结果中的一条社区乐谱;``to_dict`` 的键即 ``cgdl musescore-search`` 的 JSON 字段。"""

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

    def to_dict(self) -> dict[str, object]:
        return {
            "score_id": self.score_id,
            "url": self.url,
            "title": self.title,
            "composer": self.composer,
            "instruments": list(self.instruments),
            "pages": self.pages,
            "rating": self.rating,
            "rating_count": self.rating_count,
            "duration": self.duration,
            "license": self.license,
        }


@dataclass(frozen=True, slots=True)
class ScorePage:
    score_id: int
    url: str
    title: str
    composer: str | None
    uploader: str | None
    key: str | None
    published: str | None
    modified: str | None
    price: float | None
    currency: str | None
    thumbnail: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.score_id,
            "url": self.url,
            "title": self.title,
            "composer": self.composer,
            "uploader": self.uploader,
            "key": self.key,
            "published": self.published,
            "modified": self.modified,
            "price": self.price,
            "currency": self.currency,
            "thumbnail": self.thumbnail,
        }


def page_title(html: str) -> str:
    match = _TITLE_RE.search(html)
    return html_lib.unescape(match[1]).strip() if match else ""


def is_challenge_title(title: str) -> bool:
    return any(title.startswith(prefix) for prefix in _CHALLENGE_TITLES)


def is_challenge(html: str, cf_mitigated: str | None) -> bool:
    """Cloudflare 挑战页:响应头 cf-mitigated: challenge,或标题为挑战文案。"""
    return cf_mitigated == "challenge" or is_challenge_title(page_title(html))


def is_search_page(html: str) -> bool:
    return page_title(html).startswith(_SEARCH_TITLE_PREFIX)


def is_not_found_page(html: str) -> bool:
    return page_title(html).startswith(_NOT_FOUND_TITLE_PREFIX)


@dataclass
class _Anchor:
    score_id: int
    user_id: int
    title: str | None = None


class _Scanner(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.ld_blocks: list[str] = []
        self.stores: list[str] = []
        self.anchors: dict[int, _Anchor] = {}
        self._ld_buffer: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value or "" for name, value in attrs}
        if tag == "meta":
            key = values.get("property") or values.get("name")
            if key and "content" in values:
                self.meta.setdefault(key, values["content"])
        elif tag == "script" and values.get("type") == "application/ld+json":
            self._ld_buffer = []
        elif tag == "a":
            self._note_anchor(values.get("href", ""), values.get("aria-label"))
        if "js-store" in values.get("class", "").split() and "data-content" in values:
            self.stores.append(values["data-content"])

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._ld_buffer is not None:
            self.ld_blocks.append("".join(self._ld_buffer))
            self._ld_buffer = None

    def handle_data(self, data: str) -> None:
        if self._ld_buffer is not None:
            self._ld_buffer.append(data)

    def _note_anchor(self, href: str, label: str | None) -> None:
        try:
            parts = urlsplit(href)
        except ValueError:
            return
        if parts.hostname not in _SCORE_HOSTS:
            return
        match = _SCORE_PATH.fullmatch(parts.path)
        if match is None:
            return
        score_id = int(match[2])
        anchor = self.anchors.get(score_id)
        if anchor is None:
            anchor = _Anchor(score_id=score_id, user_id=int(match[1]))
            self.anchors[score_id] = anchor
        if label and anchor.title is None:
            anchor.title = label.strip()


def _scan(html: str) -> _Scanner:
    scanner = _Scanner()
    scanner.feed(html)
    scanner.close()
    return scanner


def parse_search_html(html: str) -> list[ScoreHit]:
    """搜索页的社区乐谱列表(去重,保持页面顺序);官方乐谱被丢弃。无结果时返回空列表。

    数据区存在时以数据区为准;数据区缺失则退回卡片锚点;两者都没有则抛 PARSE_FAILED。
    """
    nodes = _search_scores(html)
    if nodes is None:
        anchored = _anchor_hits(html)
        if not anchored:
            raise CgdlError(PARSE_FAILED, "search page has no result data")
        return anchored
    hits: list[ScoreHit] = []
    seen: set[int] = set()
    for node in nodes:
        if is_official(node):
            continue
        hit = _hit_of(node)
        if hit is None or hit.score_id in seen:
            continue
        seen.add(hit.score_id)
        hits.append(hit)
    return hits


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
    return isinstance(badges, list) and any("official" in str(badge).lower() for badge in badges)


def _search_scores(html: str) -> list[dict[str, Any]] | None:
    """数据区里的搜索结果列表;没有数据区时返回 None。"""
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
    return None


def _anchor_hits(html: str) -> list[ScoreHit]:
    hits: list[ScoreHit] = []
    for anchor in _scan(html).anchors.values():
        if anchor.title:
            hits.append(_anchor_hit(anchor.score_id, anchor.user_id, anchor.title))
    return hits


def _anchor_hit(score_id: int, user_id: int, title: str) -> ScoreHit:
    """卡片锚点只提供 id、链接与标题;其余字段页面未给出,按缺省处理。"""
    return ScoreHit(
        score_id=score_id,
        url=ScoreRef(score_id=score_id, user_id=user_id).url,
        title=_clean_title(title),
        composer=None,
        instruments=(),
        pages=None,
        rating=None,
        rating_count=None,
        duration=None,
        license="未注明",
    )


def _hit_of(node: Mapping[str, Any]) -> ScoreHit | None:
    href = _as_str(node.get("href"))
    score_id = _as_int(node.get("id"))
    title = _as_str(node.get("title"))
    if href is None or score_id is None or title is None:
        return None
    parts = urlsplit(href)
    if parts.hostname not in _SCORE_HOSTS:
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
        url=ScoreRef(score_id=score_id, user_id=int(match[1])).url,
        title=_clean_title(title),
        composer=_as_str(node.get("composer_name")),
        instruments=instruments,
        pages=_as_int(node.get("pages_count")),
        rating=rating_value,
        rating_count=rating_count,
        duration=_as_str(node.get("duration")),
        license=license_text,
    )


def _clean_title(title: str) -> str:
    return " ".join(html_lib.unescape(_BRACKET_TAG_RE.sub("", title)).split())


def page_score_id(html: str) -> int | None:
    """乐谱页 og:url 中的乐谱 id;缺失或不是乐谱路径时返回 None。"""
    url = _scan(html).meta.get("og:url")
    if url is None:
        return None
    match = _SCORE_PATH.fullmatch(_path_of(url))
    return int(match[2]) if match is not None else None


def find_first_page_png(html: str) -> str | None:
    """乐谱页内第 1 页 PNG(优先 500x660 缩略图,其次原图)。JSON 数据区里的 ``\\/`` 先还原。"""
    text = html_lib.unescape(html).replace("\\/", "/")
    for pattern in (_LARGE_PNG_RE, _ORIGINAL_PNG_RE):
        match = pattern.search(text)
        if match is not None:
            return match[0]
    return None


def parse_score_page(html: str, expected_id: int) -> ScorePage:
    """解析乐谱页元数据。页面的 score id 必须与请求的 id 一致。"""
    scan = _scan(html)
    objects = [obj for block in scan.ld_blocks for obj in _ld_objects(block)]
    composition = _first_typed(objects, "MusicComposition")
    product = _first_typed(objects, "Product")
    stored = _store_score(scan.stores, expected_id) or {}
    comp: dict[str, Any] = composition or {}

    url = scan.meta.get("og:url") or _as_str(comp.get("url")) or ""
    if not _matches_id(url, expected_id):
        raise CgdlError(PARSE_FAILED, "score page URL does not match the requested score id")

    og_title = scan.meta.get("og:title", "").split(" - ")[0].strip()
    title = (
        _as_str(stored.get("title"))
        or _as_str(stored.get("name"))
        or _as_str(comp.get("name"))
        or _as_str(og_title)
    )
    if title is None:
        raise CgdlError(PARSE_FAILED, "score title not found on page")

    offer = _first_offer(product)
    return ScorePage(
        score_id=expected_id,
        url=url,
        title=title,
        composer=(
            _name_of(stored.get("composer"))
            or _name_of(comp.get("composer"))
            or _as_str(scan.meta.get("musescore:composer"))
        ),
        uploader=_as_str(scan.meta.get("musescore:author")),
        key=_as_str(comp.get("musicalKey")),
        published=_as_str(comp.get("datePublished")),
        modified=_as_str(comp.get("dateModified")),
        price=_as_number(offer.get("price")) if offer else None,
        currency=_as_str(offer.get("priceCurrency")) if offer else None,
        thumbnail=_as_str(comp.get("thumbnailUrl")),
    )


def _store_payloads(stores: list[str]) -> list[object]:
    payloads: list[object] = []
    for raw in stores:
        try:
            payloads.append(json.loads(html_lib.unescape(raw)))
        except ValueError:
            # 数据区损坏时不致命:依赖 JSON-LD 与 meta 兜底。
            continue
    return payloads


def _walk(node: object) -> Iterator[dict[str, Any]]:
    if isinstance(node, dict):
        yield node
        for child in node.values():
            yield from _walk(child)
    elif isinstance(node, list):
        for child in node:
            yield from _walk(child)


def _store_score(stores: list[str], expected_id: int) -> dict[str, Any] | None:
    for payload in _store_payloads(stores):
        for node in _walk(payload):
            if _as_int(node.get("id")) == expected_id and ("title" in node or "name" in node):
                return node
    return None


def _ld_objects(block: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(block)
    except ValueError:
        return []
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def _first_typed(objects: list[dict[str, Any]], type_name: str) -> dict[str, Any] | None:
    for obj in objects:
        if obj.get("@type") == type_name:
            return obj
    return None


def _first_offer(product: dict[str, Any] | None) -> dict[str, Any] | None:
    if product is None:
        return None
    offers = product.get("offers")
    if isinstance(offers, dict):
        return offers
    if isinstance(offers, list) and offers and isinstance(offers[0], dict):
        return offers[0]
    return None


def _path_of(url: str) -> str:
    try:
        return urlsplit(url).path
    except ValueError:
        return ""


def _matches_id(url: str, expected_id: int) -> bool:
    match = _SCORE_PATH.fullmatch(_path_of(url))
    return match is not None and int(match[2]) == expected_id


def _name_of(value: object) -> str | None:
    if isinstance(value, dict):
        return _as_str(value.get("name"))
    return _as_str(value)


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
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None
