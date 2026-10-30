from __future__ import annotations

import json
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from cgbot.group.skill import (
    GroupSkillStore,
    InMemoryProfileSink,
    LastSession,
    MemberNote,
    MemberObservation,
    SessionSummary,
    SkillCard,
    SkillCardError,
    SkillLimits,
    merge_card,
    parse_summary,
    profile_entries,
    render_transcript,
)
from cgbot.session import Item

GID = "5001"
SID = f"group:{GID}"
LIMITS = SkillLimits(topics=3, memes=2, rules=2, members=2, habits_per_member=2)


def card_dict(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema": "cgbot.group_skill",
        "version": 1,
        "group_id": GID,
        "revision": 1,
        "updated_at": 10.0,
        "atmosphere": "轻松",
        "topics": ["游戏"],
        "memes": ["鱼"],
        "rules": [],
        "members": [{"user_id": "u1", "role": None, "habits": ["夜猫"], "last_seen": 10.0}],
        "last_session": {"session_id": SID, "ended_at": 10.0, "end_reason": "idle"},
    }
    base.update(overrides)
    return base


def summary(
    *,
    atmosphere: str = "轻松",
    topics: tuple[str, ...] = (),
    memes: tuple[str, ...] = (),
    rules: tuple[str, ...] = (),
    members: tuple[MemberObservation, ...] = (),
) -> SessionSummary:
    return SessionSummary(
        atmosphere=atmosphere,
        topics=topics,
        memes=memes,
        rules=rules,
        members=members,
    )


def test_card_round_trip_through_json() -> None:
    card = SkillCard.from_json(json.dumps(card_dict(), ensure_ascii=False))
    assert card.group_id == GID
    assert card.members == (MemberNote(user_id="u1", role=None, habits=("夜猫",), last_seen=10.0),)
    assert card.last_session == LastSession(session_id=SID, ended_at=10.0, end_reason="idle")
    again = SkillCard.from_json(card.to_json())
    assert again == card


def test_card_rejects_wrong_version_and_schema() -> None:
    with pytest.raises(SkillCardError):
        SkillCard.from_json(json.dumps(card_dict(version=2)))
    with pytest.raises(SkillCardError):
        SkillCard.from_json(json.dumps(card_dict(schema="other")))
    with pytest.raises(SkillCardError):
        SkillCard.from_json("{not json")


def test_parse_summary_strips_fences_and_rejects_bad_shape() -> None:
    raw = "```json\n" + json.dumps({"atmosphere": "好", "topics": ["a"]}, ensure_ascii=False)
    raw += "\n```"
    parsed = parse_summary(raw)
    assert parsed.atmosphere == "好"
    assert parsed.topics == ("a",)
    with pytest.raises(ValueError):
        parse_summary("[1, 2]")


def test_merge_card_creates_revision_one_and_dedupes_new_first() -> None:
    s = summary(
        topics=("Game", "game", "美食"),
        members=(MemberObservation(user_id="u1", role=None, habits=("a",)),),
    )
    card = merge_card(
        None,
        s,
        group_id=GID,
        session_id=SID,
        now=5.0,
        end_reason="end_tool",
        limits=LIMITS,
    )
    assert card.revision == 1
    assert card.topics == ("Game", "美食")  # casefold dedupe, first spelling kept
    assert card.last_session is not None and card.last_session.end_reason == "end_tool"


def test_merge_card_caps_lists_and_keeps_recent_members_first() -> None:
    prev = SkillCard.from_json(
        json.dumps(
            card_dict(
                topics=["旧1", "旧2", "旧3"],
                members=[
                    {"user_id": "old", "role": None, "habits": [], "last_seen": 1.0},
                    {"user_id": "mid", "role": None, "habits": [], "last_seen": 5.0},
                ],
            )
        )
    )
    s = summary(
        topics=("新话题",),
        members=(MemberObservation(user_id="new", role="管理", habits=("h1", "h2", "h3")),),
    )
    card = merge_card(
        prev, s, group_id=GID, session_id=SID, now=20.0, end_reason="idle", limits=LIMITS
    )
    assert card.revision == prev.revision + 1
    assert card.topics == ("新话题", "旧1", "旧2")  # capped at 3
    assert [m.user_id for m in card.members] == ["new", "mid"]  # capped at 2, recent first
    assert card.members[0].habits == ("h1", "h2")  # habits capped at 2
    assert card.members[0].last_seen == 20.0


def test_merge_card_keeps_previous_atmosphere_when_summary_empty() -> None:
    prev = SkillCard.from_json(json.dumps(card_dict(atmosphere="热闹")))
    card = merge_card(
        prev,
        summary(atmosphere=""),
        group_id=GID,
        session_id=SID,
        now=20.0,
        end_reason="mute",
        limits=LIMITS,
    )
    assert card.atmosphere == "热闹"


def test_profile_entries_carry_group_domain_and_member_habits() -> None:
    s = summary(
        members=(MemberObservation(user_id="u1", role="成员", habits=("爱吐槽",)),),
    )
    entries = profile_entries(group_id=GID, session_id=SID, ended_at=9.0, summary=s)
    assert len(entries) == 1
    assert entries[0].domain == f"group:{GID}"
    assert entries[0].user_id == "u1"
    assert entries[0].habits == ("爱吐槽",)


async def test_in_memory_profile_sink_records_entries() -> None:
    sink = InMemoryProfileSink()
    entries = profile_entries(
        group_id=GID,
        session_id=SID,
        ended_at=9.0,
        summary=summary(members=(MemberObservation(user_id="u1", role=None, habits=("x",)),)),
    )
    await sink.write(entries)
    assert sink.entries == list(entries)


def test_render_transcript_keeps_user_text_and_bot_reply_only() -> None:
    items = [
        Item(1, SID, "system_event", {"event": "group_session_started"}, 0.0),
        Item(
            2,
            SID,
            "user",
            {
                "item": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "[成员 u1] 你好"}],
                }
            },
            0.0,
        ),
        Item(
            3,
            SID,
            "tool_call",
            {
                "name": "reply",
                "call_id": "c1",
                "item": {"type": "function_call", "arguments": json.dumps({"text": "嗨"})},
            },
            0.0,
        ),
        Item(
            4,
            SID,
            "tool_call",
            {
                "name": "end_conversation",
                "call_id": "c2",
                "item": {"type": "function_call", "arguments": "{}"},
            },
            0.0,
        ),
    ]
    text = render_transcript(items)
    assert "[成员 u1] 你好" in text
    assert "[机器人回复] 嗨" in text
    assert "end_conversation" not in text
    assert "group_session_started" not in text


# ---- store -----------------------------------------------------------------------------------


@pytest.fixture
def store_path(tmp_path: Path) -> Path:
    return tmp_path / "group_skill.sqlite"


@pytest.fixture
async def store(store_path: Path) -> AsyncIterator[GroupSkillStore]:
    s = GroupSkillStore(store_path)
    yield s
    s.close()


async def test_card_round_trip_survives_reopen(store: GroupSkillStore, store_path: Path) -> None:
    card = SkillCard.from_json(json.dumps(card_dict()))
    await store.put_card(card)
    store.close()

    reopened = GroupSkillStore(store_path)
    try:
        assert await reopened.get_card(GID) == card
        assert await reopened.get_card("9999") is None
    finally:
        reopened.close()


async def test_session_ledger_open_update_finish(store: GroupSkillStore) -> None:
    row = await store.open_session(group_id=GID, session_id=SID, first_seq=7, now=1.0)
    await store.update_session(row, participants=["u1", "u2"], turn_count=3, last_activity=2.0)

    active = await store.active_sessions()
    assert len(active) == 1
    rec = active[0]
    assert (rec.group_id, rec.first_seq, rec.turn_count) == (GID, 7, 3)
    assert rec.participants == ("u1", "u2")
    assert rec.last_activity == 2.0

    await store.finish_session(row, end_reason="idle", summary_status="written", now=3.0)
    assert await store.active_sessions() == []


async def test_only_one_active_session_per_group(store: GroupSkillStore) -> None:
    await store.open_session(group_id=GID, session_id=SID, first_seq=1, now=1.0)
    with pytest.raises(sqlite3.IntegrityError):
        await store.open_session(group_id=GID, session_id=SID, first_seq=9, now=2.0)


async def test_schema_version_row_is_created(store: GroupSkillStore, store_path: Path) -> None:
    store.close()
    conn = sqlite3.connect(store_path)
    try:
        row = conn.execute(
            "SELECT version FROM schema_version WHERE component = ?", ("group_skill",)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None and row[0] == 1
    # reopen so the fixture teardown can close a live store
    reopened = GroupSkillStore(store_path)
    reopened.close()
