"""Reads the host producer's daily ``delivery.json`` and builds outbound QQ messages.

The producer writes ``<output_dir>/<YYYY-MM-DD>/delivery.json``; this module only reads it and
never triggers generation. Tiers: ``fast`` is a tall PNG, sliced into chat-sized images cut at
blank rows; ``lite`` is a PDF file; ``max`` is a short-lived OSS link (text only).
"""

from __future__ import annotations

import base64
import io
import json
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Final

from PIL import Image

from cgbot.model import Segment

TIER_NAMES: Final = ("fast", "lite", "max")
IMAGE_SLICE_HEIGHT: Final = 6000  # px; taller Fast images are cut so QQ accepts and renders them
LINK_VALID_NOTE: Final = "链接 24 小时内有效"
PARTIAL_NOTE: Final = "（今天的刊还在生成中,以上是已出的部分。）"
_DEFAULT_LABELS: Final = {"fast": "快讯图", "lite": "精简版 PDF", "max": "完整版"}


@dataclass(frozen=True, slots=True)
class Tier:
    name: str
    label: str
    path: Path | None
    file_name: str | None
    link: str | None
    error: str | None


@dataclass(frozen=True, slots=True)
class Delivery:
    issue_id: str
    complete: bool  # producer ``status == "complete"``
    tiers: tuple[Tier, ...]  # always in ``TIER_NAMES`` order

    def tier(self, name: str) -> Tier:
        for tier in self.tiers:
            if tier.name == name:
                return tier
        raise KeyError(name)


@dataclass(frozen=True, slots=True)
class IssueMessages:
    groups: tuple[tuple[Segment, ...], ...]  # one outbound QQ message per group, in order
    notes: tuple[str, ...]  # tiers left out, with the reason


def read_delivery(output_dir: Path, day: str) -> Delivery | None:
    """Read ``<output_dir>/<day>/delivery.json``; None when missing or unreadable."""
    day_dir = output_dir / day
    try:
        raw = json.loads((day_dir / "delivery.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    issue_id = _text(raw.get("issue_id")) or day
    entries = raw.get("tiers")
    if not isinstance(entries, dict):
        entries = {}
    tiers = tuple(_tier(name, entries.get(name), day_dir) for name in TIER_NAMES)
    return Delivery(issue_id=issue_id, complete=raw.get("status") == "complete", tiers=tiers)


def build_issue_messages(delivery: Delivery, tiers: Sequence[str]) -> IssueMessages:
    """Build the outbound groups for the requested tiers; unusable tiers become notes."""
    groups: list[tuple[Segment, ...]] = []
    notes: list[str] = []
    for name in TIER_NAMES:
        if name not in tiers:
            continue
        tier = delivery.tier(name)
        if name == "fast":
            part, note = _fast(tier)
        elif name == "lite":
            part, note = _lite(tier)
        else:
            part, note = _max(tier)
        groups.extend(part)
        if note is not None:
            notes.append(note)
    return IssueMessages(groups=tuple(groups), notes=tuple(notes))


def slice_png(data: bytes, max_height: int = IMAGE_SLICE_HEIGHT) -> list[bytes]:
    """Cut a tall PNG into pieces of at most ``max_height`` px, cutting at blank rows.

    Images that already fit are returned unchanged. Each cut prefers the lowest blank row in
    ``(top + max_height // 2, top + max_height]``, else the hard limit.
    """
    with Image.open(io.BytesIO(data)) as opened:
        image = opened.convert("RGB")
    width, height = image.size
    if height <= max_height:
        return [data]
    cuts = [0]
    top = 0
    while height - top > max_height:
        target = top + max_height
        cut = next(
            (
                row
                for row in range(target, max(top + max_height // 2, top + 1), -1)
                if _blank_row(image, width, row)
            ),
            target,
        )
        cuts.append(cut)
        top = cut
    cuts.append(height)
    pieces: list[bytes] = []
    for start, end in pairwise(cuts):
        buffer = io.BytesIO()
        image.crop((0, start, width, end)).save(buffer, "PNG", optimize=True)
        pieces.append(buffer.getvalue())
    return pieces


def _blank_row(image: Image.Image, width: int, row: int) -> bool:
    return image.crop((0, row, width, row + 1)).getcolors(maxcolors=1) is not None


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _tier(name: str, entry: object, day_dir: Path) -> Tier:
    fields = entry if isinstance(entry, dict) else {}
    raw_path = _text(fields.get("path"))
    path: Path | None = None
    if raw_path is not None:
        candidate = Path(raw_path)
        path = candidate if candidate.is_absolute() else day_dir / candidate
    return Tier(
        name=name,
        label=_text(fields.get("label_zh")) or _DEFAULT_LABELS[name],
        path=path,
        file_name=_text(fields.get("file_name")),
        link=_text(fields.get("link")),
        error=_text(fields.get("error")),
    )


def _usable_path(tier: Tier) -> Path | None:
    if tier.error is not None or tier.path is None or not tier.path.is_file():
        return None
    return tier.path


def _unavailable(tier: Tier) -> str:
    return f"{tier.label}暂不可用({tier.error or '未就绪'})"


def _fast(tier: Tier) -> tuple[list[tuple[Segment, ...]], str | None]:
    path = _usable_path(tier)
    if path is None:
        return [], _unavailable(tier)
    try:
        pieces = slice_png(path.read_bytes())
    except OSError:
        return [], f"{tier.label}图片无法读取"
    total = len(pieces)
    groups: list[tuple[Segment, ...]] = []
    for index, piece in enumerate(pieces, start=1):
        label = tier.label if total == 1 else f"{tier.label}({index}/{total})"
        groups.append(
            (
                Segment("text", {"text": label}),
                Segment("image", {"file": _base64_uri(piece)}),
            )
        )
    return groups, None


def _lite(tier: Tier) -> tuple[list[tuple[Segment, ...]], str | None]:
    path = _usable_path(tier)
    if path is None:
        return [], _unavailable(tier)
    try:
        data = path.read_bytes()
    except OSError:
        return [], f"{tier.label}文件无法读取"
    name = tier.file_name or path.name
    return [
        (
            Segment("text", {"text": tier.label}),
            Segment("file", {"file": _base64_uri(data), "name": name}),
        )
    ], None


def _max(tier: Tier) -> tuple[list[tuple[Segment, ...]], str | None]:
    if tier.link is None or tier.error is not None:
        return [], _unavailable(tier)
    return [(Segment("text", {"text": f"{tier.label}({LINK_VALID_NOTE}):\n{tier.link}"}),)], None


def _base64_uri(data: bytes) -> str:
    return "base64://" + base64.b64encode(data).decode("ascii")
