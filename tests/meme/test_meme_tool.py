from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from cgbot.agent import ToolContext, ToolSpec
from cgbot.meme import MemeEntry, MemeStore, meme_lookup_tool
from cgbot.meme.store import MAX_QUERY_CHARS
from cgbot.model import Channel


async def _no_send(_message: object) -> object:
    raise AssertionError("meme lookup must not send messages")


def _ctx(session_id: str, user_id: str = "10001") -> ToolContext:
    return ToolContext(
        user_id=user_id,
        session_id=session_id,
        source_channel=Channel("qq", "10001"),
        source_message_id=None,
        sender=cast(Any, _no_send),
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[MemeStore]:
    meme_store = MemeStore(tmp_path / "meme.sqlite")
    yield meme_store
    meme_store.close()


@pytest.fixture
async def seeded(store: MemeStore) -> MemeStore:
    await store.upsert_many("100", [MemeEntry("大肥鱼", "群吉祥物")], now=1.0)
    await store.upsert_many("200", [MemeEntry("小肥鱼", "另一个群的梗")], now=2.0)
    return store


async def _call(spec: ToolSpec, ctx: ToolContext, args: dict[str, Any]) -> tuple[str, bool]:
    result = await spec.handler(ctx, args)
    return result.output, result.is_error


def test_spec_name_and_only_query_parameter(store: MemeStore) -> None:
    spec = meme_lookup_tool(store)
    assert spec.name == "meme_lookup"
    assert set(spec.parameters_schema["properties"]) == {"query"}


async def test_group_session_sees_only_its_own_group(seeded: MemeStore) -> None:
    spec = meme_lookup_tool(seeded)

    output, is_error = await _call(spec, _ctx("group:100"), {"query": "肥鱼"})

    assert not is_error
    assert "大肥鱼" in output
    assert "小肥鱼" not in output


async def test_group_session_cannot_widen_scope_with_args(seeded: MemeStore) -> None:
    spec = meme_lookup_tool(seeded)

    output, is_error = await _call(spec, _ctx("group:100"), {"query": "肥鱼", "group_id": "200"})

    assert is_error
    assert "group_id" in output


async def test_owner_private_session_sees_all_groups(seeded: MemeStore) -> None:
    spec = meme_lookup_tool(seeded)

    output, is_error = await _call(spec, _ctx("private:10001"), {"query": "肥鱼"})

    assert not is_error
    assert "- 大肥鱼: 群吉祥物" in output
    assert "- 小肥鱼: 另一个群的梗" in output


async def test_other_sessions_are_refused(seeded: MemeStore) -> None:
    spec = meme_lookup_tool(seeded)

    for session_id in ("private:20002", "group:abc", "web:10001", "group:"):
        output, is_error = await _call(spec, _ctx(session_id), {"query": "肥鱼"})
        assert is_error, session_id
        assert "大肥鱼" not in output and "小肥鱼" not in output


async def test_invalid_queries_are_rejected(seeded: MemeStore) -> None:
    spec = meme_lookup_tool(seeded)
    ctx = _ctx("group:100")

    assert (await _call(spec, ctx, {"query": "   "}))[1]
    assert (await _call(spec, ctx, {}))[1]
    assert (await _call(spec, ctx, {"query": 3}))[1]
    assert (await _call(spec, ctx, {"query": "鱼" * (MAX_QUERY_CHARS + 1)}))[1]


async def test_query_at_limit_is_accepted(seeded: MemeStore) -> None:
    spec = meme_lookup_tool(seeded)

    output, is_error = await _call(spec, _ctx("group:100"), {"query": "鱼" * MAX_QUERY_CHARS})

    assert not is_error
    assert output == "词条库里没有相关记录。"


async def test_unknown_arguments_are_rejected(seeded: MemeStore) -> None:
    spec = meme_lookup_tool(seeded)

    output, is_error = await _call(
        spec, _ctx("group:100"), {"query": "肥鱼", "limit": 50, "scope": "all"}
    )

    assert is_error
    assert "limit" in output and "scope" in output


async def test_no_hits_returns_plain_message(seeded: MemeStore) -> None:
    spec = meme_lookup_tool(seeded)

    output, is_error = await _call(spec, _ctx("group:100"), {"query": "完全不存在"})

    assert not is_error
    assert output == "词条库里没有相关记录。"


async def test_entry_without_meaning_renders_term_only(store: MemeStore) -> None:
    await store.upsert_many("100", [MemeEntry("躺平", "")], now=1.0)
    spec = meme_lookup_tool(store)

    output, _ = await _call(spec, _ctx("group:100"), {"query": "躺平"})

    assert output == "- 躺平"
