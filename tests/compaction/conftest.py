from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from compaction_fakes import FakeProvider, Rows

from cgbot.session import AsyncSessionStore


@pytest.fixture
def provider_cls() -> type[FakeProvider]:
    return FakeProvider


@pytest.fixture
def rows_cls() -> type[Rows]:
    return Rows


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[AsyncSessionStore]:
    s = AsyncSessionStore(tmp_path / "sessions.sqlite", spill_dir=tmp_path / "spill")
    try:
        yield s
    finally:
        await s.close()
