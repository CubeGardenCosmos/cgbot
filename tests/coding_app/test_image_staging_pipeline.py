"""Private inbound images become sandbox references without changing group ingress."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock

import pytest
from coding_app_fakes import app_config

from cgbot.app import App
from cgbot.coding.contracts import workspace_dir
from cgbot.coding.staging import ImageStager
from cgbot.dispatch import PrivateMessage
from cgbot.model import Channel, InboundMessage, Segment
from cgbot.provider import ModelProvider, user_message

PNG = b"\x89PNG\r\n\x1a\nimage"


def _message(*, group: bool = False, file_only: bool = False) -> InboundMessage:
    return InboundMessage(
        message_id="image-message",
        scope="group" if group else "private",
        user_id="1001",
        group_id="2001" if group else None,
        segments=(
            Segment("text", {"text": "请编辑这张图"}),
            Segment("image", {"file" if file_only else "url": "https://cdn.example/image"}),
        ),
        time=0.0,
    )


@pytest.mark.parametrize("file_only", [False, True])
async def test_image_staging_private_pipeline_uses_internal_user_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, file_only: bool
) -> None:
    config = app_config(tmp_path, coding=True)
    app = App(config, cast(ModelProvider, object()))
    fetched: list[str] = []
    queued: list[PrivateMessage] = []

    async def fetch(url: str) -> bytes:
        fetched.append(url)
        return PNG

    assert config.coding is not None
    app._image_stager = ImageStager(
        config.coding.docker.host_data_dir, fetch=fetch, clock=lambda: 0
    )
    monkeypatch.setattr(app._dispatcher, "on_message", queued.append)
    try:
        await app.handle_message(_message(file_only=file_only))
        assert fetched == ["https://cdn.example/image"]
        assert len(queued) == 1
        identity = await app._identity.resolve(Channel("qq", "1001"))
        inbox = workspace_dir(config.coding.docker.host_data_dir, identity.user_id) / "inbox"
        files = list(inbox.iterdir())
        assert len(files) == 1
        assert files[0].read_bytes() == PNG
        sandbox_path = f"/workspace/inbox/{files[0].name}"
        assert queued[0].item == user_message(
            f"请编辑这张图[图片]\n[图片已保存到沙盒: {sandbox_path}]"
        )
    finally:
        await app.stop()


async def test_image_staging_disabled_pipeline_never_fetches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = App(app_config(tmp_path, coding=False), cast(ModelProvider, object()))
    queued: list[PrivateMessage] = []

    async def fetch(url: str) -> bytes:
        pytest.fail("coding disabled must not fetch inbound images")

    app._image_stager = ImageStager(tmp_path, fetch=fetch, clock=lambda: 0)
    monkeypatch.setattr(app._dispatcher, "on_message", queued.append)
    try:
        await app.handle_message(_message())
        assert len(queued) == 1
        assert queued[0].item == user_message("请编辑这张图[图片]")
    finally:
        await app.stop()


async def test_image_staging_group_pipeline_never_fetches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = App(app_config(tmp_path, coding=True), cast(ModelProvider, object()))

    async def fetch(url: str) -> bytes:
        pytest.fail("group ingress must not stage images")

    app._image_stager = ImageStager(tmp_path, fetch=fetch, clock=lambda: 0)
    group_handler = AsyncMock()
    monkeypatch.setattr(app, "_handle_group", group_handler)
    try:
        message = _message(group=True)
        await app.handle_message(message)
        group_handler.assert_awaited_once_with(message)
    finally:
        await app.stop()


async def test_image_staging_fetch_failure_preserves_message_and_hides_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    app = App(app_config(tmp_path, coding=True), cast(ModelProvider, object()))
    queued: list[PrivateMessage] = []

    async def fetch(url: str) -> bytes:
        raise OSError("sensitive-download-detail")

    app._image_stager = ImageStager(tmp_path, fetch=fetch, clock=lambda: 0)
    monkeypatch.setattr(app._dispatcher, "on_message", queued.append)
    try:
        with caplog.at_level(logging.WARNING):
            await app.handle_message(_message())
        assert len(queued) == 1
        assert queued[0].item == user_message("请编辑这张图[图片]")
        assert "image staging failed: code=" in caplog.text
        assert "sensitive-download-detail" not in caplog.text
        assert "https://cdn.example/image" not in caplog.text
        assert "请编辑这张图" not in caplog.text
    finally:
        await app.stop()
