"""History-side image references and the inbound path from segments to stored refs.

A user turn's input item keeps its ``input_text`` part and gains one ``cgbot_image`` part per
stored image (``{"type": "cgbot_image", "ref": <file name>}``). The part is internal: history
rendering turns it into a Responses ``input_image`` for recent turns and drops it otherwise, so it
never reaches a provider or a compaction request.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cgbot.config.settings import VisionConfig
from cgbot.model import Segment
from cgbot.provider.items import user_message
from cgbot.vision.fetch import Fetch, ImageError, default_fetch, fetch_image
from cgbot.vision.image import prepare_image
from cgbot.vision.store import ImageStore

log = logging.getLogger(__name__)

IMAGE_REF_PART = "cgbot_image"


def message_with_images(text: str, refs: Sequence[str]) -> dict[str, Any]:
    """``user_message(text)`` plus one ``cgbot_image`` part per ref."""
    item = user_message(text)
    item["content"].extend({"type": IMAGE_REF_PART, "ref": ref} for ref in refs)
    return item


def item_image_refs(item: Mapping[str, Any]) -> list[str]:
    """Refs of the ``cgbot_image`` parts of one input item, in order."""
    content = item.get("content")
    if not isinstance(content, list):
        return []
    return [
        str(part["ref"])
        for part in content
        if isinstance(part, Mapping) and part.get("type") == IMAGE_REF_PART and "ref" in part
    ]


def segment_image_urls(segments: Sequence[Segment]) -> list[str]:
    """URL (or ``file`` fallback) of every image segment that carries a string source."""
    urls: list[str] = []
    for segment in segments:
        if segment.type != "image":
            continue
        url = segment.data.get("url") or segment.data.get("file")
        if isinstance(url, str):
            urls.append(url)
    return urls


class ImageIntake:
    """Fetch, bound and store inbound images; never raises, never logs a URL."""

    def __init__(
        self,
        store: ImageStore,
        *,
        max_images: int,
        max_edge: int,
        fetch: Fetch = default_fetch,
    ) -> None:
        self._store = store
        self._max_images = max_images
        self._max_edge = max_edge
        self._fetch = fetch

    async def collect(self, session_id: str, urls: Sequence[str]) -> list[str]:
        """Refs of the images stored for ``session_id`` (at most ``max_images``, order kept).

        A failed image is skipped with a logged code; the caller keeps the text marker.
        """
        capped = urls[: self._max_images]
        results = await asyncio.gather(*(self._one(session_id, url) for url in capped))
        return [ref for ref in results if ref is not None]

    async def _one(self, session_id: str, url: str) -> str | None:
        try:
            raw = await fetch_image(url, self._fetch)
            prepared = await asyncio.to_thread(prepare_image, raw, self._max_edge)
            return await self._store.save(session_id, prepared.data, prepared.extension)
        except ImageError as exc:
            log.warning("chat image skipped: code=%s", exc.code)
        except Exception:
            # URLs may contain credentials; never log exception details.
            log.warning("chat image skipped: code=E_INTERNAL")
        return None


@dataclass(frozen=True, slots=True)
class Vision:
    """Enabled chat vision: what the private runner and the group sessions share."""

    intake: ImageIntake
    store: ImageStore
    recent_image_turns: int
    max_images: int

    async def attach(
        self, session_id: str, text: str, urls: Sequence[str], *, newest: bool = False
    ) -> dict[str, Any]:
        """The user input item for ``text``, with stored images for ``urls`` attached.

        ``newest`` keeps the last ``max_images`` urls instead of the first ones. Without any
        stored image this is plain ``user_message(text)``.
        """
        picked = urls[-self.max_images :] if newest else urls
        refs = await self.intake.collect(session_id, picked) if picked else []
        return message_with_images(text, refs) if refs else user_message(text)


def build_vision(
    config: VisionConfig, images_dir: Path, *, fetch: Fetch = default_fetch
) -> Vision | None:
    """The vision runtime for ``[provider.vision]``; ``None`` when disabled."""
    if not config.enabled:
        return None
    store = ImageStore(
        images_dir,
        max_files=config.retention_images,
        max_age_seconds=config.retention_days * 86400.0,
    )
    intake = ImageIntake(
        store, max_images=config.max_images_per_turn, max_edge=config.max_edge, fetch=fetch
    )
    return Vision(intake, store, config.recent_image_turns, config.max_images_per_turn)
