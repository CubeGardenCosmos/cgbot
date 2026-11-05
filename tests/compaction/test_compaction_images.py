"""Compaction treats images as the text marker and never sends image data (offline)."""

from __future__ import annotations

import json

from compaction_fakes import SID, FakeProvider, Rows

from cgbot.compaction import Compactor
from cgbot.config.settings import ReasoningTiers
from cgbot.session import AsyncSessionStore
from cgbot.vision import message_with_images


async def test_summary_request_has_the_marker_but_no_image_blobs(
    store: AsyncSessionStore,
) -> None:
    rows = Rows(store)
    await store.append(SID, "user", {"item": message_with_images("看这个[图片]", ["1-aa.jpg"])})
    await rows.assistant("好看")
    for i in range(4):
        await rows.exchange(f"问{i}", f"答{i}")
    provider = FakeProvider()

    out = await Compactor(
        provider, store, None, ReasoningTiers(), is_busy=lambda _sid: False
    ).compact(SID)

    assert out.status == "compacted"
    sent = json.dumps(provider.requests[0].input, ensure_ascii=False)
    assert "看这个[图片]" in sent
    for forbidden in ("input_image", "cgbot_image", "base64", "1-aa.jpg"):
        assert forbidden not in sent
