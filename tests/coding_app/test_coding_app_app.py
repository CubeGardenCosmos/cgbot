"""App wiring: the coding stack and its tools/prompt exist only when [coding] is set."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from coding_app_fakes import app_config

import cgbot.app as app_module
from cgbot.app import CODING_PROMPT, App
from cgbot.config.settings import SearchGeminiConfig, Secret
from cgbot.provider import ModelProvider
from cgbot.search.gemini import GeminiSearchClient
from cgbot.timefmt import time_instruction

CODEX_TOOLS = {
    "codex_submit",
    "codex_list",
    "codex_peek",
    "codex_read",
    "codex_steer",
    "codex_stop",
    "codex_compact",
    "codex_rename",
    "codex_archive",
    "codex_delete",
    "codex_fork",
    "codex_btw",
}


def _app(tmp_path: Path, *, coding: bool) -> App:
    # The provider is only stored by App; these tests never run a model turn.
    return App(app_config(tmp_path, coding=coding), cast(ModelProvider, object()))


async def test_without_coding_section_app_has_no_stack_tools_or_prompt(tmp_path: Path) -> None:
    app = _app(tmp_path, coding=False)
    try:
        assert app._coding is None
        assert all(app._runner._tools.get(name) is None for name in CODEX_TOOLS)
        assert await app._private_instructions("private:1001") == (
            f"{app._persona}\n\n{time_instruction(app._tz)}"
        )
    finally:
        await app.stop()


async def test_with_coding_section_app_registers_codex_tools_and_prompt(tmp_path: Path) -> None:
    app = _app(tmp_path, coding=True)
    try:
        assert app._coding is not None
        assert {spec.name for spec in app._coding.tools} == CODEX_TOOLS
        assert all(app._runner._tools.get(name) is not None for name in CODEX_TOOLS)
        assert await app._private_instructions("private:1001") == (
            f"{app._persona}\n\n{time_instruction(app._tz)}\n\n{CODING_PROMPT}"
        )
    finally:
        await app.stop()


async def test_private_memory_block_is_frozen_until_compaction(tmp_path: Path) -> None:
    app = _app(tmp_path, coding=False)
    try:
        sid = "private:1001"
        first = await app._private_instructions(sid)
        await app._memory.add(
            subject_user="1001", domain="private", source_domain="private", text="喜欢猫"
        )
        # Memory written mid-session must not rewrite the instructions prefix (cache stability).
        assert await app._private_instructions(sid) == first
        app._memory_blocks.pop(sid)  # what a successful compaction does
        refreshed = await app._private_instructions(sid)
        assert refreshed != first
        assert "喜欢猫" in refreshed
    finally:
        await app.stop()


async def test_stop_without_start_is_clean_with_coding_section(tmp_path: Path) -> None:
    app = _app(tmp_path, coding=True)
    await app.stop()


@pytest.mark.parametrize(
    ("upstream_key", "expected_coding_key"),
    [(Secret("coding-upstream-key"), "coding-upstream-key"), (None, "sk-test-CODING")],
)
async def test_gemini_clients_use_provider_key_for_bot_and_coding_key_for_stack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    upstream_key: Secret | None,
    expected_coding_key: str,
) -> None:
    config = app_config(tmp_path, coding=True)
    assert config.coding is not None
    config = replace(
        config,
        coding=replace(config.coding, upstream_api_key=upstream_key),
        search=replace(
            config.search,
            gemini=SearchGeminiConfig(enabled=True, base_url="https://relay.example/v1beta"),
        ),
    )
    built: list[tuple[GeminiSearchClient, tuple[object, ...]]] = []

    class RecordingClient(GeminiSearchClient):
        def __init__(self, *args: Any) -> None:
            super().__init__(*args)
            built.append((self, args))

    monkeypatch.setattr(app_module, "GeminiSearchClient", RecordingClient)
    app = App(config, cast(ModelProvider, object()))
    try:
        assert [args[:2] for _, args in built] == [
            ("https://relay.example/v1beta", "sk-test-CODING"),
            ("https://relay.example/v1beta", expected_coding_key),
        ]
        assert app._gemini is built[0][0]
        assert app._coding_gemini is built[1][0]
    finally:
        await app.stop()
