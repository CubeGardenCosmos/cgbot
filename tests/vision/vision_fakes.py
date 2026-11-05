"""Image fixtures and an offline fetch for the chat-vision tests."""

from __future__ import annotations

import io
from collections.abc import Mapping

from PIL import Image

from cgbot.vision.fetch import ImageError


def jpeg_bytes(
    width: int = 64, height: int = 48, color: tuple[int, int, int] = (200, 30, 30)
) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), color).save(out, "JPEG")
    return out.getvalue()


def png_bytes(width: int = 32, height: int = 32, *, alpha: bool = False) -> bytes:
    out = io.BytesIO()
    if alpha:
        Image.new("RGBA", (width, height), (0, 255, 0, 80)).save(out, "PNG")
    else:
        Image.new("RGB", (width, height), (0, 0, 255)).save(out, "PNG")
    return out.getvalue()


def gif_bytes(width: int = 16, height: int = 16) -> bytes:
    out = io.BytesIO()
    Image.new("P", (width, height), 3).save(out, "GIF")
    return out.getvalue()


class MapFetch:
    """Injectable fetch: url -> bytes; unknown urls fail like a download error."""

    def __init__(self, files: Mapping[str, bytes]) -> None:
        self.files = dict(files)
        self.calls: list[str] = []

    async def __call__(self, url: str) -> bytes:
        self.calls.append(url)
        try:
            return self.files[url]
        except KeyError:
            raise ImageError("download_failed") from None
