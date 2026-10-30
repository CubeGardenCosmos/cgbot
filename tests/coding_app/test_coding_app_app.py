"""App wiring: the coding stack and its tools/prompt exist only when [coding] is set."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from coding_app_fakes import app_config

from cgbot.app import CODING_PROMPT, SYSTEM_PROMPT, App
from cgbot.provider import ModelProvider

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
        assert await app._private_instructions("private:1001") == SYSTEM_PROMPT
    finally:
        await app.stop()


async def test_with_coding_section_app_registers_codex_tools_and_prompt(tmp_path: Path) -> None:
    app = _app(tmp_path, coding=True)
    try:
        assert app._coding is not None
        assert {spec.name for spec in app._coding.tools} == CODEX_TOOLS
        assert all(app._runner._tools.get(name) is not None for name in CODEX_TOOLS)
        assert await app._private_instructions("private:1001") == (
            f"{SYSTEM_PROMPT}\n\n{CODING_PROMPT}"
        )
    finally:
        await app.stop()


async def test_stop_without_start_is_clean_with_coding_section(tmp_path: Path) -> None:
    app = _app(tmp_path, coding=True)
    await app.stop()
