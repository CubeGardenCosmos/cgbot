"""Image preparation, per-scope storage with retention, and intake (offline)."""

from __future__ import annotations

import base64
import io
import logging
from pathlib import Path

import pytest
from PIL import Image
from vision_fakes import MapFetch, gif_bytes, jpeg_bytes, png_bytes

from cgbot.vision import ImageError, ImageIntake, ImageStore
from cgbot.vision.image import prepare_image

URL = "https://img.example/a.jpg"


def _decode(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data))


# ---- prepare_image ---------------------------------------------------------------------------


def test_large_photo_is_downscaled_to_max_edge_and_stays_jpeg() -> None:
    prepared = prepare_image(jpeg_bytes(4000, 3000), 1536)
    image = _decode(prepared.data)
    assert prepared.mime == "image/jpeg" and prepared.extension == "jpg"
    assert image.size == (1536, 1152)


def test_small_image_is_not_upscaled() -> None:
    assert _decode(prepare_image(jpeg_bytes(64, 48), 1536).data).size == (64, 48)


def test_png_with_alpha_stays_png_and_keeps_transparency() -> None:
    prepared = prepare_image(png_bytes(40, 40, alpha=True), 1536)
    image = _decode(prepared.data)
    assert prepared.mime == "image/png" and prepared.extension == "png"
    assert image.mode == "RGBA" and image.getchannel("A").getpixel((0, 0)) == 80


def test_opaque_png_and_gif_become_jpeg() -> None:
    assert prepare_image(png_bytes(), 1536).mime == "image/jpeg"
    assert prepare_image(gif_bytes(), 1536).mime == "image/jpeg"


@pytest.mark.parametrize(
    "data", [b"", b"not an image", b"BM" + b"\x00" * 64, b"\x89PNG\r\n\x1a\nbad"]
)
def test_non_image_or_corrupt_data_is_rejected_with_a_code(data: bytes) -> None:
    with pytest.raises(ImageError) as caught:
        prepare_image(data, 1536)
    assert caught.value.code == "unsupported_image"


# ---- store: scope isolation and retention ----------------------------------------------------


class Clock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _store(tmp_path: Path, clock: Clock, *, files: int = 50, days: float = 30) -> ImageStore:
    return ImageStore(
        tmp_path / "images", max_files=files, max_age_seconds=days * 86400, clock=clock
    )


async def test_save_then_load_returns_a_data_url_of_the_stored_bytes(tmp_path: Path) -> None:
    store = _store(tmp_path, Clock())
    ref = await store.save("private:u1", b"\xff\xd8\xffJPEGDATA", "jpg")
    loaded = await store.load("private:u1", [ref])
    assert loaded == {
        ref: "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xffJPEGDATA").decode()
    }


async def test_a_ref_never_resolves_outside_its_owning_scope(tmp_path: Path) -> None:
    store = _store(tmp_path, Clock())
    group_ref = await store.save("group:5001", b"G", "jpg")
    private_ref = await store.save("private:u1", b"P", "png")

    assert await store.load("private:u1", [group_ref]) == {}
    assert await store.load("group:5002", [group_ref]) == {}
    assert await store.load("private:u2", [private_ref]) == {}
    assert await store.load("group:5001", [private_ref]) == {}
    assert list(await store.load("group:5001", [group_ref])) == [group_ref]


async def test_unsafe_refs_and_sessions_are_ignored(tmp_path: Path) -> None:
    store = _store(tmp_path, Clock())
    ref = await store.save("private:u1", b"P", "jpg")
    outside = tmp_path / "images" / "secret.jpg"
    outside.write_bytes(b"x")

    assert await store.load("private:u1", [f"../{ref}", "../secret.jpg", "/etc/passwd", ""]) == {}
    assert await store.load("system:x", [ref]) == {}
    assert await store.load("private:../u1", [ref]) == {}
    with pytest.raises(ValueError):
        await store.save("private:../u1", b"x", "jpg")


async def test_retention_keeps_newest_files_per_scope(tmp_path: Path) -> None:
    clock = Clock()
    store = _store(tmp_path, clock, files=3)
    refs: list[str] = []
    for _ in range(5):
        clock.now += 1
        refs.append(await store.save("private:u1", b"x", "jpg"))
    other = await store.save("group:5001", b"y", "jpg")

    left = await store.load("private:u1", refs)
    assert list(left) == refs[2:]
    assert list(await store.load("group:5001", [other])) == [other]  # other scope untouched


async def test_retention_removes_files_older_than_max_age_and_prune_covers_all_scopes(
    tmp_path: Path,
) -> None:
    clock = Clock()
    store = _store(tmp_path, clock, days=1)
    old_private = await store.save("private:u1", b"x", "jpg")
    old_group = await store.save("group:5001", b"x", "jpg")
    clock.now += 2 * 86400
    fresh = await store.save("private:u1", b"x", "jpg")  # save prunes its own scope only

    assert await store.load("private:u1", [old_private]) == {}
    assert list(await store.load("group:5001", [old_group])) == [old_group]

    assert await store.prune() == 1
    assert await store.load("group:5001", [old_group]) == {}
    assert list(await store.load("private:u1", [fresh])) == [fresh]


async def test_prune_on_a_missing_root_is_a_noop(tmp_path: Path) -> None:
    assert await _store(tmp_path, Clock()).prune() == 0


# ---- intake ----------------------------------------------------------------------------------


def _intake(
    tmp_path: Path, fetch: MapFetch, *, max_images: int = 4, max_edge: int = 1536
) -> tuple[ImageIntake, ImageStore]:
    store = _store(tmp_path, Clock())
    return ImageIntake(store, max_images=max_images, max_edge=max_edge, fetch=fetch), store


async def test_intake_stores_a_bounded_image_per_url(tmp_path: Path) -> None:
    fetch = MapFetch({URL: jpeg_bytes(3000, 2000)})
    intake, store = _intake(tmp_path, fetch, max_edge=512)

    refs = await intake.collect("private:u1", [URL])

    assert len(refs) == 1
    url = (await store.load("private:u1", refs))[refs[0]]
    assert url.startswith("data:image/jpeg;base64,")
    raw = base64.b64decode(url.split(",", 1)[1])
    assert _decode(raw).size == (512, 341)


async def test_intake_failure_is_skipped_and_logs_a_code_only(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    secret_url = "https://img.example/a.jpg?token=SECRET-TOKEN"
    fetch = MapFetch(
        {"https://img.example/ok.jpg": jpeg_bytes(), "https://img.example/bad": b"nope"}
    )
    intake, _ = _intake(tmp_path, fetch)

    with caplog.at_level(logging.WARNING):
        refs = await intake.collect(
            "private:u1", [secret_url, "https://img.example/bad", "https://img.example/ok.jpg"]
        )

    assert len(refs) == 1  # only the good one survives, in order
    assert "code=download_failed" in caplog.text and "code=unsupported_image" in caplog.text
    assert "SECRET-TOKEN" not in caplog.text and "img.example" not in caplog.text


async def test_intake_rejects_private_addresses_before_fetching(tmp_path: Path) -> None:
    fetch = MapFetch({"http://127.0.0.1/a.jpg": jpeg_bytes()})
    intake, _ = _intake(tmp_path, fetch)

    assert (
        await intake.collect("private:u1", ["http://127.0.0.1/a.jpg", "file:///etc/passwd"]) == []
    )
    assert fetch.calls == []


async def test_intake_caps_images_per_turn(tmp_path: Path) -> None:
    urls = [f"https://img.example/{i}.jpg" for i in range(6)]
    fetch = MapFetch({u: jpeg_bytes() for u in urls})
    intake, _ = _intake(tmp_path, fetch, max_images=2)

    assert len(await intake.collect("private:u1", urls)) == 2
    assert fetch.calls == urls[:2]
