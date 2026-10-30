from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiohttp.web
import httpx
import pytest
from aiohttp.test_utils import TestClient, TestServer
from imagegen_fakes import (
    SECRET,
    USER,
    RecordingProvider,
    b64,
    chat_reply,
    make_config,
    output_dir,
    owner_calls,  # noqa: F401  (registers the `owners` fixture)
    png_bytes,
    workspace,
)

from cgbot.coding.imagegen import ImageGenService, imagegen_routes


class StaticAuth:
    """Maps bearer tokens to user ids, like SandboxAuth.resolve."""

    def __init__(self, tokens: dict[str, str]) -> None:
        self._tokens = tokens

    def token_for(self, user_id: str) -> str:
        return f"tok-{user_id}"

    def resolve(self, token: str) -> str | None:
        return self._tokens.get(token)


AUTH = StaticAuth({"tok-alice": USER})
PNG = png_bytes(64, 48)
ALICE = {"Authorization": "Bearer tok-alice"}


@asynccontextmanager
async def _serve(service: ImageGenService) -> AsyncIterator[TestClient]:
    app = aiohttp.web.Application()
    app.add_routes(imagegen_routes(service, AUTH))
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()


def _service(
    root: Path, respond, *, configured: bool = True
) -> tuple[ImageGenService, RecordingProvider]:
    provider = RecordingProvider(respond)
    config = make_config(root, configured=configured)
    return ImageGenService(config, transport=provider.transport()), provider


def _ok(_req: httpx.Request) -> httpx.Response:
    return chat_reply(f"![x](data:image/png;base64,{b64(PNG)})")


@pytest.fixture
async def ok_client(tmp_path: Path) -> AsyncIterator[tuple[TestClient, RecordingProvider]]:
    workspace(tmp_path)
    service, provider = _service(tmp_path, _ok)
    async with _serve(service) as client:
        yield client, provider


async def test_missing_or_unknown_bearer_is_unauthorized(
    ok_client: tuple[TestClient, RecordingProvider],
) -> None:
    client, provider = ok_client
    no_header = await client.post("/bridge/imagegen", json={"prompt": "a cat"})
    assert no_header.status == 401
    assert (await no_header.json())["error"] == "unauthorized"

    unknown = await client.post(
        "/bridge/imagegen", json={"prompt": "a cat"}, headers={"Authorization": "Bearer nope"}
    )
    assert unknown.status == 401
    assert provider.requests == []


async def test_success_returns_result_and_takes_identity_from_token(
    tmp_path: Path, owners: list[tuple[str, int, int]]
) -> None:
    workspace(tmp_path)
    workspace(tmp_path, "bob")
    service, provider = _service(tmp_path, _ok)
    async with _serve(service) as client:
        response = await client.post(
            "/bridge/imagegen",
            json={"prompt": "a cat", "name": "from_route", "user_id": "bob"},
            headers=ALICE,
        )
        assert response.status == 200
        body = await response.json()

    assert set(body) == {"path", "width", "height", "bytes", "size"}
    assert body["path"] == "/workspace/artifacts/imagegen/from_route.png"
    assert (body["width"], body["height"]) == (64, 48)
    assert (output_dir(tmp_path) / "from_route.png").read_bytes() == PNG
    assert not output_dir(tmp_path, "bob").exists()
    assert len(provider.posts()) == 1


@pytest.mark.parametrize("payload", [b"{not json", b"[1, 2]", b'"just a string"'])
async def test_non_object_or_invalid_json_is_bad_request(
    ok_client: tuple[TestClient, RecordingProvider], payload: bytes
) -> None:
    client, provider = ok_client
    response = await client.post(
        "/bridge/imagegen",
        data=payload,
        headers={**ALICE, "Content-Type": "application/json"},
    )
    assert response.status == 400
    assert (await response.json())["error"] == "invalid_request"
    assert provider.requests == []


@pytest.mark.parametrize(
    "payload",
    [
        {"prompt": 42},
        {"prompt": "a cat", "size": 1024},
        {"prompt": "a cat", "reference_images": "/workspace/x.png"},
        {"prompt": "a cat", "reference_images": [1]},
    ],
)
async def test_field_type_errors_are_bad_request(
    ok_client: tuple[TestClient, RecordingProvider], payload: dict[str, object]
) -> None:
    client, provider = ok_client
    response = await client.post("/bridge/imagegen", json=payload, headers=ALICE)
    assert response.status == 400
    assert (await response.json())["error"] == "invalid_request"
    assert provider.requests == []


async def test_not_configured_maps_to_503(tmp_path: Path) -> None:
    workspace(tmp_path)
    service, provider = _service(tmp_path, _ok, configured=False)
    async with _serve(service) as client:
        response = await client.post("/bridge/imagegen", json={"prompt": "a cat"}, headers=ALICE)
        body = await response.json()
    assert response.status == 503
    assert body["error"] == "not_configured"
    assert provider.requests == []


async def test_provider_failure_maps_to_502_without_secret(tmp_path: Path) -> None:
    workspace(tmp_path)

    def refuse(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"refused {SECRET}")

    service, provider = _service(tmp_path, refuse)
    async with _serve(service) as client:
        response = await client.post("/bridge/imagegen", json={"prompt": "a cat"}, headers=ALICE)
        text = await response.text()
    assert response.status == 502
    assert json.loads(text)["error"] == "generation_failed"
    assert SECRET not in text
    assert len(provider.posts()) == 1
