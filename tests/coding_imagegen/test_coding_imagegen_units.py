from __future__ import annotations

import io

import pytest
from imagegen_fakes import b64, jpeg_bytes, png_bytes
from PIL import Image

from cgbot.coding import imagegen as ig
from cgbot.coding.config import ImageGenConfig
from cgbot.config.settings import Secret


@pytest.mark.parametrize("text", ["1024x1024", "1536x1024", "512x1536", "16x16", "3840x1280"])
def test_parse_size_accepts_valid_sizes(text: str) -> None:
    assert ig.parse_size(text) == text


@pytest.mark.parametrize(
    "text",
    ["1000x1000", "8x8", "0x0", "16x1024", "4096x4096", "2048x512", "1024", "axb", "1024x1024x1"],
)
def test_parse_size_rejects_invalid_sizes(text: str) -> None:
    with pytest.raises(ig._InvalidImage):
        ig.parse_size(text)


@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        (None, "1024x1024"),
        ((512, 512), "512x512"),
        ((3000, 2000), "1536x1024"),
        ((2000, 3000), "1024x1536"),
        ((6000, 100), "1536x512"),
    ],
)
def test_resolve_size_follows_reference_shape(
    reference: tuple[int, int] | None, expected: str
) -> None:
    assert ig.resolve_size(reference) == expected


def test_extract_payload_chat_markdown_data_url() -> None:
    result = {"choices": [{"message": {"content": "here ![x](data:image/png;base64,QUJD)"}}]}
    assert ig.extract_payload(result, images_mode=False) == b"ABC"


def test_extract_payload_chat_bare_url_and_content_parts() -> None:
    bare = {"choices": [{"message": {"content": "done https://cdn.example/a.png ok"}}]}
    assert ig.extract_payload(bare, images_mode=False) == "https://cdn.example/a.png"
    parts = {
        "choices": [
            {
                "message": {
                    "content": [
                        {"type": "text", "text": "x"},
                        {"type": "image_url", "image_url": {"url": "https://cdn.example/b.png"}},
                    ]
                }
            }
        ]
    }
    assert ig.extract_payload(parts, images_mode=False) == "https://cdn.example/b.png"


def test_extract_payload_images_b64_and_url() -> None:
    assert ig.extract_payload({"data": [{"b64_json": "QUJD"}]}, images_mode=True) == b"ABC"
    assert (
        ig.extract_payload({"data": [{"url": "https://cdn.example/c.png"}]}, images_mode=True)
        == "https://cdn.example/c.png"
    )


@pytest.mark.parametrize(
    ("result", "images_mode"),
    [
        ({"choices": []}, False),
        ({"choices": [{"message": {"content": "no picture here"}}]}, False),
        ({"data": []}, True),
        ({"data": [{"b64_json": "!!!!"}]}, True),
        ([], False),
    ],
)
def test_extract_payload_rejects_responses_without_image(result: object, images_mode: bool) -> None:
    with pytest.raises(ig._GenerationFailed):
        ig.extract_payload(result, images_mode=images_mode)


def _config(mode: str) -> ImageGenConfig:
    return ImageGenConfig(
        base_url="https://provider.example/v1",
        api_key=Secret("k"),
        model="m",
        mode=mode,
        timeout_seconds=5.0,
        proxy="",
        max_concurrency=1,
    )


def test_build_request_chat_mode_carries_size_hint_and_references() -> None:
    image = ig._ProviderImage(data=b"\x89PNG\r\n\x1a\nxx", mime="image/png")
    path, body = ig.build_request(_config("chat"), "a cat", [image], "1536x1024")
    assert path == "/chat/completions"
    assert body["model"] == "m"
    content = body["messages"][0]["content"]
    assert "approximately 1536x1024" in content[0]["text"]
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_build_request_images_mode_generation_and_edit_endpoints() -> None:
    path, body = ig.build_request(_config("images"), "a cat", [], "1024x1024")
    assert path == "/images/generations"
    assert body == {"model": "m", "prompt": "a cat", "size": "1024x1024"}
    image = ig._ProviderImage(data=b"\xff\xd8\xffxx", mime="image/jpeg")
    path, body = ig.build_request(_config("images"), "a cat", [image], "1024x1024")
    assert path == "/images/edits"
    assert body["image"] == ["data:image/jpeg;base64," + b64(b"\xff\xd8\xffxx")]


def test_normalize_png_reencodes_jpeg_and_keeps_png() -> None:
    data, width, height = ig.normalize_png(jpeg_bytes(64, 48))
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    assert (width, height) == (64, 48)
    original = png_bytes(8, 8)
    assert ig.normalize_png(original) == (original, 8, 8)


def test_normalize_png_rejects_non_image_bytes() -> None:
    with pytest.raises(ig._GenerationFailed):
        ig.normalize_png(b"definitely not an image")


def test_inspect_image_accepts_png_and_jpeg_with_metadata() -> None:
    data = png_bytes(32, 16)
    info = ig.inspect_image(data)
    assert (info.mime, info.width, info.height, info.size) == ("image/png", 32, 16, len(data))
    assert ig.inspect_image(jpeg_bytes(20, 20)).mime == "image/jpeg"


@pytest.mark.parametrize(
    "data",
    [
        b"GIF89a" + b"\x00" * 20,
        b"%PDF-1.4 minimal",
        b"\x89PNG\r\n\x1a\n" + b"garbage that is not a png",
    ],
)
def test_inspect_image_rejects_unsupported_or_fake_images(data: bytes) -> None:
    with pytest.raises(ig._InvalidImage):
        ig.inspect_image(data)


def test_inspect_image_rejects_oversized_dimensions() -> None:
    buffer = io.BytesIO()
    Image.new("L", (ig.MAX_DIMENSION + 1, 1)).save(buffer, "PNG")
    with pytest.raises(ig._InvalidImage, match="dimensions"):
        ig.inspect_image(buffer.getvalue())


def test_decode_data_url_rejects_bad_base64() -> None:
    with pytest.raises(ig._GenerationFailed):
        ig._decode_data_url("data:image/png;base64,!!!!")
