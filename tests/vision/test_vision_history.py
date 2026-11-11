"""Image turns in history and in the model request: recent-N expansion, scope isolation."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from vision_fakes import MapFetch, jpeg_bytes

from cgbot.agent import AgentRunner, TurnContext, builtin_tools
from cgbot.agent.history import expand_window, recent_image_refs, render_items, to_input_items
from cgbot.model import Channel, OutboundMessage, SendResult
from cgbot.provider import ModelRequest, ModelResponse, TextOutput, Usage
from cgbot.session import AsyncSessionStore, Item
from cgbot.timefmt import format_stamp
from cgbot.vision import ImageIntake, ImageStore, message_with_images

URL = "https://img.example/{}.jpg"
TZ = ZoneInfo("Asia/Shanghai")


def _rows(*texts_and_refs: tuple[str, list[str]]) -> list[Item]:
    return [
        Item(
            seq=i + 1,
            session_id="private:u1",
            kind="user",
            payload={"item": message_with_images(text, refs)},
            ts=float(i),
        )
        for i, (text, refs) in enumerate(texts_and_refs)
    ]


def _image_parts(items: list[dict[str, Any]]) -> list[list[str]]:
    return [
        [p["image_url"] for p in item["content"] if p["type"] == "input_image"] for item in items
    ]


def test_only_the_newest_image_turns_are_expanded_older_ones_keep_the_marker() -> None:
    rows = _rows(
        ("a[图片]", ["r1.jpg"]),
        ("纯文字", []),
        ("b[图片]", ["r2.jpg", "r3.jpg"]),
        ("c[图片]", ["r4.jpg"]),
    )
    window = expand_window(rows)

    assert recent_image_refs(window, 2) == ["r2.jpg", "r3.jpg", "r4.jpg"]  # text-only turn skipped
    assert recent_image_refs(window, 0) == []

    urls = {ref: f"data:image/jpeg;base64,{ref}" for ref in recent_image_refs(window, 2)}
    items = render_items(window, urls, tz=TZ)
    assert _image_parts(items) == [
        [],
        [],
        ["data:image/jpeg;base64,r2.jpg", "data:image/jpeg;base64,r3.jpg"],
        ["data:image/jpeg;base64,r4.jpg"],
    ]
    # the degraded turn still says "[图片]"; part 0 is the timestamp
    assert items[0]["content"][1]["text"] == "a[图片]"
    assert "cgbot_image" not in json.dumps(items)


def test_unresolved_refs_are_dropped_not_leaked() -> None:
    items = to_input_items(_rows(("x[图片]", ["gone.jpg"])), {}, tz=TZ)
    assert items[0]["content"] == [
        {"type": "input_text", "text": f"{format_stamp(0.0, TZ)} "},
        {"type": "input_text", "text": "x[图片]"},
    ]


# ---- through the runner ----------------------------------------------------------------------


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


async def _send(message: OutboundMessage) -> SendResult:
    return SendResult(ok=True, message_id="m", error=None)


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[AsyncSessionStore]:
    s = AsyncSessionStore(tmp_path / "sessions.sqlite", spill_dir=tmp_path / "spill")
    yield s
    await s.close()


def _turn(session_id: str, item: dict[str, Any], n: int) -> TurnContext:
    return TurnContext(
        user_id="u1",
        session_id=session_id,
        source_channel=Channel("qq", "10001"),
        source_message_id=f"m{n}",
        new_items=[item],
        reason="user",
    )


async def test_request_carries_input_image_for_recent_turns_only(
    tmp_path: Path, store: AsyncSessionStore
) -> None:
    images = ImageStore(tmp_path / "images", max_files=50, max_age_seconds=86400)
    urls = [URL.format(i) for i in range(5)]
    intake = ImageIntake(
        images, max_images=4, max_edge=256, fetch=MapFetch({u: jpeg_bytes() for u in urls})
    )
    provider = Provider()
    runner = AgentRunner(
        provider, store, builtin_tools(), "sys", _send, images=images, recent_image_turns=3, tz=TZ
    )

    for n, url in enumerate(urls):
        refs = await intake.collect("private:u1", [url])
        await runner.run_turn(_turn("private:u1", message_with_images(f"图{n}[图片]", refs), n))

    last = [i for i in provider.requests[-1].input if i.get("role") == "user"]
    per_turn = _image_parts(last)
    assert [len(p) for p in per_turn] == [0, 0, 1, 1, 1]  # newest three expanded
    assert all(u.startswith("data:image/jpeg;base64,") for p in per_turn for u in p)
    assert [i["content"][1]["text"] for i in last] == [f"图{n}[图片]" for n in range(5)]
    # the first request of the run saw exactly one image (its own turn)
    assert [len(p) for p in _image_parts(provider.requests[0].input)] == [1]


async def test_group_images_never_appear_in_a_private_request(
    tmp_path: Path, store: AsyncSessionStore
) -> None:
    images = ImageStore(tmp_path / "images", max_files=50, max_age_seconds=86400)
    group_ref = await images.save("group:5001", jpeg_bytes(), "jpg")
    provider = Provider()
    runner = AgentRunner(
        provider, store, builtin_tools(), "sys", _send, images=images, recent_image_turns=3, tz=TZ
    )

    # A private history item that (wrongly or maliciously) names a group-owned ref.
    await runner.run_turn(_turn("private:u1", message_with_images("私聊[图片]", [group_ref]), 1))

    sent = json.dumps(provider.requests[0].input)
    assert "input_image" not in sent and "base64" not in sent and group_ref not in sent
    assert [len(p) for p in _image_parts(provider.requests[0].input)] == [0]


async def test_without_a_vision_source_refs_are_dropped(
    tmp_path: Path, store: AsyncSessionStore
) -> None:
    provider = Provider()
    runner = AgentRunner(provider, store, builtin_tools(), "sys", _send, tz=TZ)
    await runner.run_turn(_turn("private:u1", message_with_images("x[图片]", ["r.jpg"]), 1))
    stamp, text = provider.requests[0].input[0]["content"]
    assert stamp["text"].startswith("[") and stamp["text"].endswith("] ")
    assert text == {"type": "input_text", "text": "x[图片]"}
