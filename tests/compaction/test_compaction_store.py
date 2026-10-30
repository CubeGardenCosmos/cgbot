from __future__ import annotations

import pytest
from compaction_fakes import SID, Rows

from cgbot.config.settings import ReasoningTiers
from cgbot.provider import user_message
from cgbot.provider.tiers import effort_for
from cgbot.session import AsyncSessionStore


async def test_append_if_last_commits_when_the_tail_matches(
    store: AsyncSessionStore, rows_cls: type[Rows]
) -> None:
    seq = await rows_cls(store).user("第一句")
    new_seq = await store.append_if_last(SID, seq, "user", {"item": user_message("第二句")})
    assert new_seq is not None and new_seq > seq
    assert [i.seq for i in await store.load(SID)] == [seq, new_seq]


async def test_append_if_last_on_empty_session_expects_zero(store: AsyncSessionStore) -> None:
    seq = await store.append_if_last(SID, 0, "user", {"item": user_message("开场")})
    assert seq is not None
    assert await store.append_if_last(SID, 0, "user", {"item": user_message("重复")}) is None


async def test_append_if_last_mismatch_inserts_nothing(
    store: AsyncSessionStore, rows_cls: type[Rows]
) -> None:
    rows = rows_cls(store)
    seq = await rows.user("第一句")
    await rows.user("抢先的一句")
    before = await store.load(SID)
    result = await store.append_if_last(SID, seq, "user", {"item": user_message("迟到")})
    assert result is None
    assert await store.load(SID) == before


async def test_append_if_last_refuses_tool_results(store: AsyncSessionStore) -> None:
    payload = {"call_id": "c1", "name": "f", "output": "r", "is_error": False}
    with pytest.raises(ValueError):
        await store.append_if_last(SID, 0, "tool_result", payload)
    assert await store.load(SID) == []


def test_effort_for_defaults_match_the_design() -> None:
    tiers = ReasoningTiers()
    assert effort_for("summary", tiers) == "low"
    assert effort_for("private", tiers) == "high"
    assert effort_for("planning", tiers) == "max"
    assert effort_for("group", tiers) == "low"


def test_effort_for_passes_configured_values_through() -> None:
    tiers = ReasoningTiers(summary="medium", private="xhigh", planning="none", group="minimal")
    assert effort_for("summary", tiers) == "medium"
    assert effort_for("private", tiers) == "xhigh"
    assert effort_for("planning", tiers) == "none"
    assert effort_for("group", tiers) == "minimal"


def test_effort_for_rejects_unknown_purpose() -> None:
    with pytest.raises(ValueError):
        effort_for("chitchat", ReasoningTiers())  # type: ignore[arg-type]
