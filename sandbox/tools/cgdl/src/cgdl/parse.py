"""HTML 与 JSON 解析(纯函数,无网络):Cloudflare 挑战识别、搜索结果、乐谱页元数据。

数据来源优先级:页面内嵌的 `.js-store[data-content]` JSON -> schema.org JSON-LD -> meta/og 标签。
搜索页的卡片 `<a aria-label=... href=.../user/<uid>/scores/<id>>` 作为 DOM 兜底。
"""

from __future__ import annotations

import html as html_lib
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit

from cgdl.errors import PARSE_FAILED, CgdlError

_CHALLENGE_TITLES = ("Just a moment", "Attention Required")
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_SCORE_PATH = re.compile(r"/user/(\d{1,12})/scores/(\d{1,12})/?")
_SCORE_HOSTS = frozenset({None, "musescore.com", "www.musescore.com"})
_SEARCH_TITLE_PREFIX = "Search result for"
_NOT_FOUND_TITLE_PREFIX = "Page not found"


@dataclass(frozen=True, slots=True)
class SearchHit:
    score_id: int
    user_id: int
    url: str
    title: str
    thumbnail: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.score_id,
            "user_id": self.user_id,
            "title": self.title,
            "url": self.url,
            "thumbnail": self.thumbnail,
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
    thumbnail: str | None = None


class _Scanner(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.ld_blocks: list[str] = []
        self.stores: list[str] = []
        self.anchors: dict[int, _Anchor] = {}
        self._ld_buffer: list[str] | None = None
        self._open_anchor: _Anchor | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value or "" for name, value in attrs}
        if tag == "meta":
            key = values.get("property") or values.get("name")
            if key and "content" in values:
                self.meta.setdefault(key, values["content"])
        elif tag == "script" and values.get("type") == "application/ld+json":
            self._ld_buffer = []
        elif tag == "a":
            self._open_anchor = self._note_anchor(values.get("href", ""), values.get("aria-label"))
        elif (
            tag == "img"
            and self._open_anchor is not None
            and self._open_anchor.thumbnail is None
            and values.get("src")
        ):
            self._open_anchor.thumbnail = values["src"]
        if "js-store" in values.get("class", "").split() and "data-content" in values:
            self.stores.append(values["data-content"])

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._ld_buffer is not None:
            self.ld_blocks.append("".join(self._ld_buffer))
            self._ld_buffer = None
        elif tag == "a":
            self._open_anchor = None

    def handle_data(self, data: str) -> None:
        if self._ld_buffer is not None:
            self._ld_buffer.append(data)

    def _note_anchor(self, href: str, label: str | None) -> _Anchor | None:
        try:
            parts = urlsplit(href)
        except ValueError:
            return None
        if parts.hostname not in _SCORE_HOSTS:
            return None
        match = _SCORE_PATH.fullmatch(parts.path)
        if match is None:
            return None
        score_id = int(match[2])
        anchor = self.anchors.get(score_id)
        if anchor is None:
            anchor = _Anchor(score_id=score_id, user_id=int(match[1]))
            self.anchors[score_id] = anchor
        if label and anchor.title is None:
            anchor.title = label.strip()
        return anchor


def _scan(html: str) -> _Scanner:
    scanner = _Scanner()
    scanner.feed(html)
    scanner.close()
    return scanner


def parse_search(html: str) -> list[SearchHit]:
    """从搜索页提取乐谱卡片(去重,保持页面顺序)。无结果时返回空列表。"""
    scan = _scan(html)
    hits: dict[int, SearchHit] = {}
    for anchor in scan.anchors.values():
        if anchor.title:
            hits[anchor.score_id] = _hit(
                anchor.score_id, anchor.user_id, anchor.title, anchor.thumbnail
            )
    for hit in _store_search_hits(scan.stores):
        hits.setdefault(hit.score_id, hit)
    return list(hits.values())


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


def _hit(score_id: int, user_id: int, title: str, thumbnail: str | None) -> SearchHit:
    return SearchHit(
        score_id=score_id,
        user_id=user_id,
        url=f"https://musescore.com/user/{user_id}/scores/{score_id}",
        title=title,
        thumbnail=thumbnail,
    )


def _store_payloads(stores: list[str]) -> list[object]:
    payloads: list[object] = []
    for raw in stores:
        try:
            payloads.append(json.loads(html_lib.unescape(raw)))
        except ValueError:
            # 数据区损坏时不致命:依赖 DOM/JSON-LD 兜底。
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


def _store_search_hits(stores: list[str]) -> list[SearchHit]:
    hits: list[SearchHit] = []
    for payload in _store_payloads(stores):
        for node in _walk(payload):
            score_id = _as_int(node.get("id"))
            url = _as_str(node.get("url"))
            title = _as_str(node.get("title")) or _as_str(node.get("name"))
            if score_id is None or url is None or title is None:
                continue
            match = _SCORE_PATH.fullmatch(_path_of(url))
            if match is None:
                continue
            hits.append(_hit(score_id, int(match[1]), title, _as_str(node.get("thumbnail"))))
    return hits


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
    if isinstance(value, str) and value.isdigit():
        return int(value)
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
