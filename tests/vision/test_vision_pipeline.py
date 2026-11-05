"""Inbound image segments -> model request through the real App paths (offline).

Private: ``App.handle_message`` -> dispatcher -> runner. Group: ``GroupSessionManager.handle`` as
``App`` wires it. The provider is scripted and the image fetch is injected; nothing hits a network.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from vision_fakes import MapFetch, jpeg_bytes

from cgbot.app import App
from cgbot.config import Config, load_config
from cgbot.dispatch import QUIET_SECONDS
from cgbot.model import InboundMessage, OutboundMessage, Segment, SendResult
from cgbot.provider import ModelRequest, ModelResponse, TextOutput, Usage

BASE = """
[napcat]
port = 6199

[provider]
base_url = "https://up.example/v1"
model = "model-x"
context_window = 128000
api_key_ref = "provider_api_key"

[scope]
friends = [1001]
groups = [2001]
"""
USER = "1001"
GID = "2001"
PRIVATE_URL = "https://img.example/private.jpg"
GROUP_URL = "https://img.example/group.jpg"


def make_config(tmp_path: Path, vision: str) -> Config:
    secrets = tmp_path / "secrets.toml"
    secrets.write_text('[secrets]\nprovider_api_key = "sk-test"\n', encoding="utf-8")
    secrets.chmod(0o600)
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        BASE + f'\n[storage]\ndata_dir = "{tmp_path / "data"}"\n' + vision, encoding="utf-8"
    )
    return load_config(cfg, secrets, env={})


class FakeTimebase:
    def __init__(self) -> None:
        self._now = 0.0
        self._sleepers: list[tuple[float, asyncio.Future[None]]] = []

    def now(self) -> float:
        return self._now

    async def sleep(self, seconds: float) -> None:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._sleepers.append((self._now + seconds, future))
        await future

    def advance(self, seconds: float) -> None:
        self._now += seconds
        for deadline, future in self._sleepers:
            if deadline <= self._now and not future.done():
                future.set_result(None)
        self._sleepers = [(d, f) for d, f in self._sleepers if not f.done()]


class Provider:
    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    @property
    def name(self) -> str:
        return "scripted"

    @property
    def context_window(self) -> int:
        return 100_000

    async def respond(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        raw = {"type": "message", "role": "assistant", "content": []}
        return ModelResponse(
            items=(TextOutput(text="ok", raw=raw),),
            usage=Usage(),
            response_id="r",
            status="completed",
        )

    def respond_stream(self, request: ModelRequest) -> AsyncIterator[Any]:
        raise NotImplementedError


async def _noop_send(message: OutboundMessage) -> SendResult:
    return SendResult(ok=True, message_id="out", error=None)


def private_image_msg(mid: str, text: str, *urls: str) -> InboundMessage:
    segments = [Segment("text", {"text": text})] if text else []
    segments += [Segment("image", {"url": u}) for u in urls]
    return InboundMessage(
        message_id=mid,
        scope="private",
        user_id=USER,
        group_id=None,
        segments=tuple(segments),
        time=1.0,
        is_friend=True,
    )


def group_image_msg(mid: str, text: str, *urls: str) -> InboundMessage:
    segments = [Segment("text", {"text": text})] + [Segment("image", {"url": u}) for u in urls]
    return InboundMessage(
        message_id=mid,
        scope="group",
        user_id="777",
        group_id=GID,
        segments=tuple(segments),
        time=1.0,
    )


async def settle(app: App, clock: FakeTimebase) -> None:
    for _ in range(100):
        tasks = [
            lane.task
            for lane in app._dispatcher._lanes.values()  # noqa: SLF001 - test drains lanes
            if lane.task is not None
        ]
        if not tasks:
            return
        clock.advance(QUIET_SECONDS)
        await asyncio.wait(tasks, timeout=0.5)
    raise AssertionError("app did not settle")


def image_urls_of(request: ModelRequest) -> list[str]:
    return [
        part["image_url"]
        for item in request.input
        for part in item.get("content", [])
        if isinstance(part, dict) and part.get("type") == "input_image"
    ]


ENABLED = "\n[provider.vision]\nenabled = true\nrecent_image_turns = 2\nmax_edge = 256\n"


def build(tmp_path: Path, vision: str, fetch: MapFetch) -> tuple[App, Provider, FakeTimebase]:
    clock = FakeTimebase()
    provider = Provider()
    app = App(make_config(tmp_path, vision), provider, timebase=clock, image_fetch=fetch)
    app.napcat.send = _noop_send  # type: ignore[method-assign]
    return app, provider, clock


async def test_private_image_segment_reaches_the_request_as_input_image(tmp_path: Path) -> None:
    app, provider, clock = build(tmp_path, ENABLED, MapFetch({PRIVATE_URL: jpeg_bytes(900, 600)}))

    await app.handle_message(private_image_msg("m1", "看这个", PRIVATE_URL))
    await settle(app, clock)

    item = provider.requests[0].input[0]
    assert item["content"][0] == {"type": "input_text", "text": "看这个[图片]"}
    assert item["content"][1]["type"] == "input_image"
    assert item["content"][1]["image_url"].startswith("data:image/jpeg;base64,")
    assert "cgbot_image" not in json.dumps(provider.requests[0].input)
    await app.stop()


async def test_private_fetch_failure_keeps_the_marker_and_the_turn_runs(tmp_path: Path) -> None:
    app, provider, clock = build(tmp_path, ENABLED, MapFetch({}))

    await app.handle_message(private_image_msg("m1", "看这个", PRIVATE_URL))
    await settle(app, clock)

    assert provider.requests[0].input[0]["content"] == [
        {"type": "input_text", "text": "看这个[图片]"}
    ]
    await app.stop()


async def test_disabled_vision_never_fetches_and_keeps_text_only(tmp_path: Path) -> None:
    fetch = MapFetch({PRIVATE_URL: jpeg_bytes()})
    app, provider, clock = build(tmp_path, "", fetch)

    await app.handle_message(private_image_msg("m1", "看这个", PRIVATE_URL))
    await settle(app, clock)

    assert fetch.calls == []
    assert provider.requests[0].input[0]["content"] == [
        {"type": "input_text", "text": "看这个[图片]"}
    ]
    await app.stop()


async def test_group_wake_attaches_the_image_and_private_requests_never_see_it(
    tmp_path: Path,
) -> None:
    fetch = MapFetch({PRIVATE_URL: jpeg_bytes(), GROUP_URL: jpeg_bytes(100, 100, (1, 2, 3))})
    app, provider, clock = build(tmp_path, ENABLED, fetch)

    await app._groups.handle(  # noqa: SLF001 - the gate step is covered elsewhere
        group_image_msg("g1", "群友发图", GROUP_URL), "wake"
    )
    group_request = provider.requests[-1]
    assert len(image_urls_of(group_request)) == 1
    assert group_request.input[-1]["content"][0]["text"] == "[成员 777] 群友发图[图片]"

    await app.handle_message(private_image_msg("m1", "私聊文字"))  # text only, no image
    await settle(app, clock)
    private_request = provider.requests[-1]
    assert private_request is not group_request
    assert image_urls_of(private_request) == []
    assert "群友发图" not in json.dumps(private_request.input, ensure_ascii=False)
    await app.stop()


async def test_only_the_recent_image_turns_are_sent_in_private(tmp_path: Path) -> None:
    urls = [f"https://img.example/{i}.jpg" for i in range(3)]
    fetch = MapFetch({u: jpeg_bytes(64 + i, 64) for i, u in enumerate(urls)})
    app, provider, clock = build(tmp_path, ENABLED, fetch)  # recent_image_turns = 2

    for i, url in enumerate(urls):
        await app.handle_message(private_image_msg(f"m{i}", f"图{i}", url))
        await settle(app, clock)

    last = provider.requests[-1]
    texts_with_images = [
        (item["content"][0]["text"], sum(1 for p in item["content"] if p["type"] == "input_image"))
        for item in last.input
        if item.get("role") == "user"
    ]
    assert texts_with_images == [("图0[图片]", 0), ("图1[图片]", 1), ("图2[图片]", 1)]
    await app.stop()


async def test_group_proactive_attaches_the_newest_buffered_images(tmp_path: Path) -> None:
    from cgbot.group import BufferedMessage

    urls = [f"https://img.example/{i}.jpg" for i in range(6)]
    fetch = MapFetch({u: jpeg_bytes() for u in urls})
    app, provider, _ = build(
        tmp_path, ENABLED.replace("recent_image_turns = 2", "max_images_per_turn = 2"), fetch
    )
    buffered = [
        BufferedMessage(
            message_id=f"b{i}", user_id="8", ts=1.0, text=f"图{i}[图片]", image_urls=(u,)
        )
        for i, u in enumerate(urls)
    ]

    await app._groups.handle(  # noqa: SLF001
        group_image_msg("g1", "", *[]), "proactive", buffered=buffered
    )

    assert fetch.calls == urls[-2:]  # newest two only; older ones never fetched
    assert len(image_urls_of(provider.requests[-1])) == 2
    await app.stop()
