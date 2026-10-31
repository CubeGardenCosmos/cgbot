"""Messages starting with "/" belong to other bots; only /reset reaches the model."""

from __future__ import annotations

from pathlib import Path

import pytest
from omp_fakes import quiet_watchers
from test_app import (
    IN_SCOPE,
    FakeTimebase,
    Recorder,
    ScriptedProvider,
    install,
    make_config,
    private_msg,
    settle,
    text_resp,
)

from cgbot.app import App, _is_foreign_command
from cgbot.model import Segment


@pytest.mark.parametrize(
    ("text", "ignored"),
    [
        ("/订阅 新闻", True),
        ("  /新闻采集 fast", True),
        ("/help", True),
        ("/reset", False),
        ("/RESET", False),
        ("/reset 一下", False),
        ("/resetall", True),
        ("你好 /订阅", False),
        ("", False),
    ],
)
def test_is_foreign_command_text(text: str, ignored: bool) -> None:
    assert _is_foreign_command((Segment("text", {"text": text}),)) is ignored


def test_leading_at_and_reply_are_skipped_but_images_are_not_commands() -> None:
    at = Segment("at", {"qq": "1"})
    assert _is_foreign_command((at, Segment("text", {"text": " /订阅 新闻"}))) is True
    assert _is_foreign_command((at, Segment("text", {"text": " /reset"}))) is False
    assert _is_foreign_command((Segment("image", {}), Segment("text", {"text": "/x"}))) is False


async def test_private_slash_command_gets_no_turn_but_reset_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    quiet_watchers(monkeypatch)
    clock = FakeTimebase()
    provider = ScriptedProvider(text_resp("好"))
    app = App(make_config(tmp_path), provider, timebase=clock)
    install(app, Recorder())

    await app.handle_message(private_msg("m1", IN_SCOPE, "/订阅 新闻"))
    await settle(app, clock)
    assert provider.requests == []

    await app.handle_message(private_msg("m2", IN_SCOPE, "/reset"))
    await settle(app, clock)
    assert len(provider.requests) == 1
    await app.stop()
