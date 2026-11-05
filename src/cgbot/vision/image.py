"""Bound inbound chat images before they are stored and sent to the model.

Decodes with Pillow (PNG/JPEG/WebP/GIF only, first frame), applies EXIF orientation, shrinks the
longest edge to ``max_edge`` and re-encodes: PNG when the image has transparency, JPEG otherwise.
CPU-bound; call it through ``asyncio.to_thread``.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, ImageOps

from cgbot.vision.fetch import ImageError, sniff_extension

MAX_PIXELS = 80_000_000
JPEG_QUALITY = 85
_FORMATS = ("PNG", "JPEG", "WEBP", "GIF")


@dataclass(frozen=True, slots=True)
class PreparedImage:
    mime: str
    data: bytes

    @property
    def extension(self) -> str:
        return "png" if self.mime == "image/png" else "jpg"


def prepare_image(data: bytes, max_edge: int) -> PreparedImage:
    """Magic-sniff, decode, downscale and re-encode. Raises ``ImageError`` with a stable code."""
    sniff_extension(data)
    try:
        with Image.open(io.BytesIO(data), formats=_FORMATS) as source:
            if source.width * source.height > MAX_PIXELS:
                raise ImageError("too_large")
            image = ImageOps.exif_transpose(source)
            has_alpha = image.mode in ("RGBA", "LA", "PA") or (
                image.mode == "P" and "transparency" in image.info
            )
            image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
            out = io.BytesIO()
            if has_alpha:
                image.convert("RGBA").save(out, "PNG", optimize=True)
                return PreparedImage("image/png", out.getvalue())
            image.convert("RGB").save(out, "JPEG", quality=JPEG_QUALITY, optimize=True)
            return PreparedImage("image/jpeg", out.getvalue())
    except ImageError:
        raise
    except Exception:
        raise ImageError("unsupported_image") from None
