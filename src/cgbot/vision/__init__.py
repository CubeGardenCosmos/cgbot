"""Chat vision: bounded image intake, per-scope image storage and history refs."""

from cgbot.vision.fetch import ImageError
from cgbot.vision.intake import (
    IMAGE_REF_PART,
    ImageIntake,
    Vision,
    build_vision,
    item_image_refs,
    message_with_images,
    segment_image_urls,
)
from cgbot.vision.store import ImageStore

__all__ = [
    "IMAGE_REF_PART",
    "ImageError",
    "ImageIntake",
    "ImageStore",
    "Vision",
    "build_vision",
    "item_image_refs",
    "message_with_images",
    "segment_image_urls",
]
