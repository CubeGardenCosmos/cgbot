from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import stat
from pathlib import Path

import httpx
import pytest
from imagegen_fakes import (
    BASE_URL,
    SECRET,
    USER,
    RecordingProvider,
    b64,
    chat_reply,
    images_reply,
    jpeg_bytes,
    make_config,
    output_dir,
    owner_calls,  # noqa: F401  (registers the `owners` fixture)
    png_bytes,
    workspace,
)

from cgbot.coding import imagegen as ig
from cgbot.coding.imagegen import ImageGenError, ImageGenService

CHAT_PNG = png_bytes(64, 48)


def _raise(exc: Exception) -> httpx.Response:
    raise exc


def _png_reply(data: bytes = CHAT_PNG) -> httpx.Response:
    return chat_reply(f"![img](data:image/png;base64,{b64(data)})")


def _service(
    root: Path, respond, *, mode: str = "chat", max_concurrency: int = 2
) -> tuple[ImageGenService, RecordingProvider]:
    provider = RecordingProvider(respond)
    config = make_config(root, mode=mode, max_concurrency=max_concurrency)
    return ImageGenService(config, transport=provider.transport()), provider


async def test_generate_stores_png_index_and_permissions(
    tmp_path: Path, owners: list[tuple[str, int, int]]
) -> None:
    workspace(tmp_path)
    service, provider = _service(tmp_path, lambda _req: _png_reply())
    try:
        result = await service.generate(USER, "a red square", name="square")
    finally:
        await service.close()

    assert result == ig.ImageResult(
        path="/workspace/artifacts/imagegen/square.png",
        width=64,
        height=48,
        bytes=len(CHAT_PNG),
        size="1024x1024",
    )
    stored = output_dir(tmp_path) / "square.png"
    assert stored.read_bytes() == CHAT_PNG
    assert stat.S_IMODE(stored.stat().st_mode) == 0o644
    assert ("fchown", 1000, 1000) in owners

    record = json.loads((output_dir(tmp_path) / "index.jsonl").read_text().splitlines()[0])
    assert record["path"] == result.path
    assert record["prompt"] == "a red square"
    assert record["sha256"] == hashlib.sha256(CHAT_PNG).hexdigest()
    assert record["reference_sha256"] == []
    assert record["size"] == "1024x1024"
    assert record["model"] == "test-image-model"

    request = provider.posts()[0]
    assert request.url == httpx.URL(f"{BASE_URL}/chat/completions")
    assert request.headers["Authorization"] == f"Bearer {SECRET}"
    body = json.loads(request.content)
    assert body["model"] == "test-image-model"
    assert "approximately 1024x1024" in body["messages"][0]["content"][0]["text"]


async def test_generate_rejects_existing_name_without_calling_provider(tmp_path: Path) -> None:
    workspace(tmp_path)
    output_dir(tmp_path).mkdir(parents=True)
    (output_dir(tmp_path) / "taken.png").write_bytes(b"old")
    service, provider = _service(tmp_path, lambda _req: _png_reply())

    with pytest.raises(ImageGenError) as info:
        await service.generate(USER, "a cat", name="taken")
    assert info.value.code == "invalid_request"
    assert info.value.status == 400
    assert provider.requests == []


@pytest.mark.parametrize(
    ("prompt", "size", "name"),
    [
        ("   ", None, None),
        ("x" * (ig.MAX_PROMPT_CHARS + 1), None, None),
        ("a cat", "1000x1000", None),
        ("a cat", None, "Bad Name"),
        ("a cat", None, "../escape"),
    ],
)
async def test_generate_rejects_invalid_arguments_before_provider(
    tmp_path: Path, prompt: str, size: str | None, name: str | None
) -> None:
    workspace(tmp_path)
    service, provider = _service(tmp_path, lambda _req: _png_reply())

    with pytest.raises(ImageGenError) as info:
        await service.generate(USER, prompt, size=size, name=name)
    assert info.value.code == "invalid_request"
    assert provider.requests == []


async def test_generate_rejects_invalid_user_id(tmp_path: Path) -> None:
    service, provider = _service(tmp_path, lambda _req: _png_reply())
    with pytest.raises(ImageGenError) as info:
        await service.generate("../bob", "a cat")
    assert info.value.code == "invalid_request"
    assert provider.requests == []


async def test_reference_image_sets_size_and_is_sent_with_lineage(
    tmp_path: Path, owners: list[tuple[str, int, int]]
) -> None:
    ref_dir = workspace(tmp_path) / "inputs"
    ref_dir.mkdir()
    ref_bytes = png_bytes(3000, 2000)
    (ref_dir / "ref.png").write_bytes(ref_bytes)
    service, provider = _service(tmp_path, lambda _req: _png_reply())

    result = await service.generate(
        USER, "restyle it", name="styled", reference_images=["/workspace/inputs/ref.png"]
    )

    assert result.size == "1536x1024"
    body = json.loads(provider.posts()[0].content)
    text, image = body["messages"][0]["content"]
    assert "approximately 1536x1024" in text["text"]
    assert image["image_url"]["url"] == f"data:image/png;base64,{b64(ref_bytes)}"
    record = json.loads((output_dir(tmp_path) / "index.jsonl").read_text().splitlines()[0])
    assert record["reference_sha256"] == [hashlib.sha256(ref_bytes).hexdigest()]


async def test_explicit_size_overrides_reference_shape(tmp_path: Path) -> None:
    ref_dir = workspace(tmp_path) / "inputs"
    ref_dir.mkdir()
    (ref_dir / "ref.jpg").write_bytes(jpeg_bytes(2000, 3000))
    service, _provider = _service(tmp_path, lambda _req: _png_reply())

    result = await service.generate(
        USER, "x", size="512x512", reference_images=["/workspace/inputs/ref.jpg"]
    )
    assert result.size == "512x512"


async def test_images_mode_uses_edits_endpoint_with_reference(tmp_path: Path) -> None:
    ref_dir = workspace(tmp_path) / "inputs"
    ref_dir.mkdir()
    (ref_dir / "ref.png").write_bytes(png_bytes(64, 64))
    service, provider = _service(
        tmp_path,
        lambda _req: images_reply({"b64_json": b64(CHAT_PNG)}),
        mode="images",
    )

    await service.generate(USER, "edit", reference_images=["/workspace/inputs/ref.png"])

    request = provider.posts()[0]
    assert request.url.path == "/v1/images/edits"
    assert len(json.loads(request.content)["image"]) == 1


async def test_images_mode_downloads_url_result(tmp_path: Path) -> None:
    workspace(tmp_path)

    def respond(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, content=CHAT_PNG)
        return images_reply({"url": "https://cdn.example/out.png"})

    service, provider = _service(tmp_path, respond, mode="images")
    result = await service.generate(USER, "a cat", name="from_url")

    assert result.path == "/workspace/artifacts/imagegen/from_url.png"
    gets = provider.gets()
    assert len(gets) == 1
    assert "authorization" not in {k.lower() for k in gets[0].headers}


async def test_url_download_failure_is_outcome_unknown_with_one_fetch(tmp_path: Path) -> None:
    workspace(tmp_path)

    def respond(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(500)
        return chat_reply("https://cdn.example/out.png")

    service, provider = _service(tmp_path, respond)
    with pytest.raises(ImageGenError) as info:
        await service.generate(USER, "a cat")

    assert info.value.code == "outcome_unknown"
    assert len(provider.posts()) == 1
    assert len(provider.gets()) == 1


@pytest.mark.parametrize(
    ("respond", "code"),
    [
        (lambda _r: _raise(httpx.ConnectError("refused")), "generation_failed"),
        (lambda _r: _raise(httpx.ReadTimeout("slow")), "outcome_unknown"),
        (lambda _r: _raise(httpx.RemoteProtocolError("reset")), "outcome_unknown"),
        (lambda _r: httpx.Response(504), "outcome_unknown"),
        (lambda _r: httpx.Response(408), "outcome_unknown"),
        (lambda _r: httpx.Response(524), "outcome_unknown"),
        (lambda _r: httpx.Response(500), "generation_failed"),
        (lambda _r: httpx.Response(200, content=b"<html>not json</html>"), "generation_failed"),
        (lambda _r: chat_reply("no picture here"), "generation_failed"),
        (lambda _r: httpx.Response(200, json={"choices": []}), "generation_failed"),
        (lambda _r: chat_reply("![x](data:image/png;base64,!!!!)"), "generation_failed"),
        (
            lambda _r: chat_reply(f"![x](data:image/png;base64,{b64(b'not an image')})"),
            "generation_failed",
        ),
    ],
)
async def test_provider_failures_map_to_codes_without_retry(
    tmp_path: Path, respond, code: str
) -> None:
    workspace(tmp_path)
    service, provider = _service(tmp_path, respond)

    with pytest.raises(ImageGenError) as info:
        await service.generate(USER, "a cat")

    assert info.value.code == code
    assert info.value.status == 502
    assert len(provider.posts()) == 1
    if code == "outcome_unknown":
        assert "do not retry automatically" in info.value.message


async def test_oversized_provider_response_is_outcome_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace(tmp_path)
    monkeypatch.setattr(ig, "MAX_RESPONSE_BYTES", 100)
    service, _provider = _service(tmp_path, lambda _r: httpx.Response(200, content=b"x" * 200))

    with pytest.raises(ImageGenError) as info:
        await service.generate(USER, "a cat")
    assert info.value.code == "outcome_unknown"


async def test_busy_for_same_user_and_too_many_across_users(tmp_path: Path) -> None:
    for user in (USER, "bob", "carol"):
        workspace(tmp_path, user)
    gate = asyncio.Event()
    both_arrived = asyncio.Event()
    arrived = 0

    async def respond(_req: httpx.Request) -> httpx.Response:
        nonlocal arrived
        arrived += 1
        if arrived == 2:
            both_arrived.set()
        await gate.wait()
        return _png_reply()

    service, provider = _service(tmp_path, respond, max_concurrency=2)
    first = asyncio.create_task(service.generate(USER, "one"))
    second = asyncio.create_task(service.generate("bob", "two"))
    try:
        async with asyncio.timeout(5):
            await both_arrived.wait()

        with pytest.raises(ImageGenError) as busy:
            await service.generate(USER, "again")
        assert (busy.value.status, busy.value.code) == (409, "busy")

        with pytest.raises(ImageGenError) as too_many:
            await service.generate("carol", "three")
        assert (too_many.value.status, too_many.value.code) == (429, "too_many")
    finally:
        gate.set()
        await asyncio.gather(first, second)
        await service.close()


async def test_unconfigured_service_refuses_generation(tmp_path: Path) -> None:
    workspace(tmp_path)
    provider = RecordingProvider(lambda _r: _png_reply())
    service = ImageGenService(
        make_config(tmp_path, configured=False), transport=provider.transport()
    )

    with pytest.raises(ImageGenError) as info:
        await service.generate(USER, "a cat")
    assert (info.value.status, info.value.code) == (503, "not_configured")
    assert provider.requests == []


@pytest.mark.parametrize(
    ("path", "setup"),
    [
        ("/workspace/../bob/ref.png", None),
        ("relative/ref.png", None),
        ("/etc/passwd", None),
        ("/workspace/missing.png", None),
        ("/workspace/doc.pdf", "pdf"),
        ("/workspace/anim.gif", "gif"),
        ("/workspace/fake.png", "fake"),
        ("/workspace/link.png", "symlink_file"),
        ("/workspace/linkdir/ref.png", "symlink_dir"),
        ("/workspace/hard.png", "hardlink"),
        ("/workspace/a/../../bob/x.png", None),
    ],
)
async def test_reference_paths_are_confined_to_own_workspace(
    tmp_path: Path, path: str, setup: str | None
) -> None:
    own = workspace(tmp_path)
    bob_png = workspace(tmp_path, "bob") / "ref.png"
    bob_png.write_bytes(png_bytes(16, 16))
    if setup == "pdf":
        (own / "doc.pdf").write_bytes(b"%PDF-1.4 x")
    elif setup == "gif":
        (own / "anim.gif").write_bytes(b"GIF89a" + b"\x00" * 20)
    elif setup == "fake":
        (own / "fake.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"junk")
    elif setup == "symlink_file":
        os.symlink(bob_png, own / "link.png")
    elif setup == "symlink_dir":
        os.symlink(workspace(tmp_path, "bob"), own / "linkdir")
    elif setup == "hardlink":
        os.link(bob_png, own / "hard.png")
    service, provider = _service(tmp_path, lambda _r: _png_reply())

    with pytest.raises(ImageGenError) as info:
        await service.generate(USER, "a cat", reference_images=[path])

    assert info.value.code == "invalid_request"
    assert provider.requests == []


async def test_secret_never_appears_in_logs_or_errors(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    workspace(tmp_path)
    caplog.set_level(logging.DEBUG)
    service, _provider = _service(tmp_path, lambda _r: httpx.Response(500, text=SECRET))

    with pytest.raises(ImageGenError) as info:
        await service.generate(USER, "a cat")

    assert SECRET not in caplog.text
    assert SECRET not in info.value.message
    assert SECRET not in str(info.value)
