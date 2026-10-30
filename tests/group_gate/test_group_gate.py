from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cgbot.config.settings import GroupConfig, ScopeConfig
from cgbot.group.gate import GateAction, GateDecision, GatePolicy, GateReason, GroupGate
from cgbot.group.store import GroupStateStore
from cgbot.model import InboundMessage, Segment

GROUP = "G1"
OTHER_GROUP = "G2"
BOT = "999"
TZ = timezone(timedelta(hours=8))
DAY0 = datetime(2026, 10, 30, tzinfo=TZ)


def at(day: int = 0, hour: int = 0, minute: int = 0, second: int = 0) -> float:
    return (DAY0 + timedelta(days=day, hours=hour, minutes=minute, seconds=second)).timestamp()


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class ScriptedRng:
    """Scripted draws, then ``default``. ``calls`` records each ``randrange`` stop value."""

    def __init__(self, values: tuple[int, ...] = (), default: int = 1) -> None:
        self.values = list(values)
        self.default = default
        self.calls: list[int] = []

    def randrange(self, stop: int, /) -> int:
        self.calls.append(stop)
        if self.values:
            return self.values.pop(0)
        return self.default


class Harness:
    def __init__(
        self,
        path: Path,
        *,
        rolls: tuple[int, ...] = (),
        tz: timezone | None = TZ,
        policy: GatePolicy | None = None,
        groups: frozenset[str] = frozenset({GROUP, OTHER_GROUP}),
    ) -> None:
        self.path = path
        self.tz = tz
        self.policy = policy
        self.groups = groups
        self.clock = Clock(at())
        self.rng = ScriptedRng(rolls)
        self.store = GroupStateStore(path)
        self.gate = self._build()
        self._count = 0

    def _build(self) -> GroupGate:
        return GroupGate(
            scope=ScopeConfig(owner=None, friends=frozenset(), groups=self.groups),
            group=GroupConfig(wake_keywords=("大肥鱼",), mute_keywords=("闭嘴",)),
            bot_id=BOT,
            store=self.store,
            clock=self.clock,
            rng=self.rng,
            tz=self.tz,
            policy=self.policy,
        )

    def reopen(self) -> None:
        self.store.close()
        self.store = GroupStateStore(self.path)
        self.gate = self._build()

    async def send(
        self,
        when: float,
        text: str = "",
        *,
        at_bot: bool = False,
        reply_to: str | None = None,
        user: str = "u1",
        group: str = GROUP,
    ) -> GateDecision:
        self.clock.now = when
        self._count += 1
        segments: list[Segment] = []
        if at_bot:
            segments.append(Segment("at", {"qq": BOT}))
        if text:
            segments.append(Segment("text", {"text": text}))
        msg = InboundMessage(
            message_id=f"m{self._count}",
            scope="group",
            user_id=user,
            group_id=group,
            segments=tuple(segments),
            time=when,
            reply_to=reply_to,
        )
        return await self.gate.decide(msg)


def decided(action: GateAction, reason: GateReason) -> GateDecision:
    return GateDecision(action, reason)


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return Harness(tmp_path / "group_state.sqlite")


# --- wake ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "at_bot", "expected"),
    [
        ("大肥鱼 在吗", True, decided("wake", "wake_keyword")),
        ("大肥鱼 在吗", False, decided("ignore", "roll_miss")),
        ("在吗", True, decided("ignore", "roll_miss")),
        ("", True, decided("ignore", "roll_miss")),
    ],
)
async def test_wake_needs_at_and_keyword(
    harness: Harness, text: str, at_bot: bool, expected: GateDecision
) -> None:
    assert await harness.send(at(hour=10), text, at_bot=at_bot) == expected


async def test_wake_keyword_matches_inside_longer_text(
    harness: Harness,
) -> None:
    assert await harness.send(at(hour=10), "Hi 大肥鱼", at_bot=True) == decided(
        "wake", "wake_keyword"
    )


# --- sleep --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("hour", "minute", "second", "expected"),
    [
        (0, 0, 0, decided("ignore", "sleep")),
        (3, 0, 0, decided("ignore", "sleep")),
        (7, 59, 59, decided("ignore", "sleep")),
        (8, 0, 0, decided("wake", "wake_keyword")),
        (23, 59, 59, decided("wake", "wake_keyword")),
    ],
)
async def test_sleep_window_is_silent_even_for_at(
    harness: Harness, hour: int, minute: int, second: int, expected: GateDecision
) -> None:
    assert (
        await harness.send(at(hour=hour, minute=minute, second=second), "大肥鱼", at_bot=True)
        == expected
    )


async def test_sleep_messages_are_not_buffered_nor_roll(harness: Harness) -> None:
    await harness.send(at(hour=3), "晚安")
    await harness.send(at(hour=7, minute=59), "还没醒", at_bot=True)
    assert harness.store.buffer(GROUP) == ()
    assert harness.rng.calls == []


# --- scope and self --------------------------------------------------------------------------


async def test_out_of_scope_group_is_ignored_and_not_stored(harness: Harness) -> None:
    harness.groups = frozenset({GROUP})
    harness.gate = harness._build()
    decision = await harness.send(at(hour=10), "大肥鱼", at_bot=True, group=OTHER_GROUP)
    assert decision == decided("ignore", "out_of_scope")
    assert harness.store.buffer(OTHER_GROUP) == ()


async def test_bot_self_message_is_ignored(harness: Harness) -> None:
    assert await harness.send(at(hour=10), "大肥鱼", at_bot=True, user=BOT) == decided(
        "ignore", "bot_self"
    )
    assert harness.store.buffer(GROUP) == ()


async def test_private_message_is_rejected(harness: Harness) -> None:
    msg = InboundMessage(
        message_id="p1",
        scope="private",
        user_id="u1",
        group_id=None,
        segments=(Segment("text", {"text": "hi"}),),
        time=at(hour=10),
    )
    with pytest.raises(ValueError):
        await harness.gate.decide(msg)


# --- mute ---------------------------------------------------------------------------------


async def test_mute_trigger_then_silence_until_exact_72h_expiry(harness: Harness) -> None:
    assert await harness.send(at(hour=10), "闭嘴", at_bot=True) == decided(
        "mute_triggered", "mute_keyword"
    )
    assert await harness.send(at(hour=10, minute=1), "大肥鱼", at_bot=True) == decided(
        "ignore", "muted"
    )
    assert await harness.send(at(hour=10, minute=2), "plain") == decided("ignore", "muted")
    assert harness.store.buffer(GROUP) == ()
    assert harness.gate.is_muted(GROUP) is True

    assert await harness.send(
        at(day=3, hour=9, minute=59, second=59), "大肥鱼", at_bot=True
    ) == decided("ignore", "muted")
    assert harness.gate.is_muted(GROUP) is True

    assert await harness.send(at(day=3, hour=10), "大肥鱼", at_bot=True) == decided(
        "wake", "wake_keyword"
    )
    assert harness.gate.is_muted(GROUP) is False


async def test_mute_keyword_without_addressing_is_plain_chat(harness: Harness) -> None:
    decision = await harness.send(at(hour=10), "闭嘴")
    assert decision == decided("ignore", "roll_miss")
    assert harness.gate.is_muted(GROUP) is False
    assert [m.text for m in harness.store.buffer(GROUP)] == ["闭嘴"]


async def test_reply_to_bot_message_counts_as_addressing(harness: Harness) -> None:
    harness.gate.note_bot_spoke(GROUP, at(hour=9), message_id="B1")
    assert await harness.send(at(hour=10), "闭嘴", reply_to="B1") == decided(
        "mute_triggered", "mute_keyword"
    )


async def test_reply_to_unknown_message_is_not_addressing(harness: Harness) -> None:
    assert await harness.send(at(hour=10), "闭嘴", reply_to="unknown") == decided(
        "ignore", "roll_miss"
    )
    assert harness.gate.is_muted(GROUP) is False


async def test_reply_to_bot_message_in_other_group_does_not_count(harness: Harness) -> None:
    harness.gate.note_bot_spoke(OTHER_GROUP, at(hour=9), message_id="B2")
    assert await harness.send(at(hour=10), "闭嘴", reply_to="B2") == decided("ignore", "roll_miss")


async def test_bot_message_without_recorded_id_is_not_addressing(harness: Harness) -> None:
    harness.gate.note_bot_spoke(GROUP, at(hour=9))
    assert await harness.send(at(hour=10), "闭嘴", reply_to="B1") == decided("ignore", "roll_miss")


async def test_mute_triggered_during_sleep_is_ignored_and_does_not_extend(
    harness: Harness,
) -> None:
    assert await harness.send(at(hour=10), "闭嘴", at_bot=True) == decided(
        "mute_triggered", "mute_keyword"
    )
    assert await harness.send(at(day=1, hour=3), "闭嘴", at_bot=True) == decided("ignore", "sleep")
    assert await harness.send(at(day=1, hour=10), "plain") == decided("ignore", "muted")
    # Original expiry at day 3 10:00 still holds; a sleep-time mute did not extend it.
    assert harness.gate.is_muted(GROUP) is True
    assert await harness.send(at(day=3, hour=9, minute=59, second=59)) == decided("ignore", "muted")
    assert await harness.send(at(day=3, hour=10), "大肥鱼", at_bot=True) == decided(
        "wake", "wake_keyword"
    )


async def test_mute_clears_followup_and_buffer(harness: Harness) -> None:
    harness.gate.note_bot_spoke(GROUP, at(hour=10))
    assert await harness.send(at(hour=10, minute=0, second=30), "plain") == decided(
        "followup", "member_reply"
    )
    assert await harness.send(at(hour=10, minute=1), "闭嘴", at_bot=True) == decided(
        "mute_triggered", "mute_keyword"
    )
    assert harness.gate.note_other_reply(GROUP, at(hour=10, minute=1, second=30)) is False
    assert harness.store.buffer(GROUP) == ()


async def test_mute_survives_restart(harness: Harness) -> None:
    await harness.send(at(hour=10), "闭嘴", at_bot=True)
    harness.reopen()
    assert harness.gate.is_muted(GROUP) is True
    assert await harness.send(at(hour=10, minute=5), "大肥鱼", at_bot=True) == decided(
        "ignore", "muted"
    )


# --- proactive roll -----------------------------------------------------------------------


async def test_proactive_hit_then_hour_and_cooldown_rules(tmp_path: Path) -> None:
    harness = Harness(tmp_path / "s.sqlite", rolls=(0, 1))
    assert await harness.send(at(hour=9), "开场白") == decided("proactive", "roll_hit")
    assert await harness.send(at(hour=9, minute=30), "同一小时") == decided("ignore", "cooldown")
    assert await harness.send(at(hour=10), "冷却中") == decided("ignore", "cooldown")
    assert await harness.send(at(hour=20, minute=59, second=59), "冷却末") == decided(
        "ignore", "cooldown"
    )
    assert await harness.send(at(hour=21), "冷却结束") == decided("ignore", "roll_miss")
    assert await harness.send(at(hour=21, minute=30), "同一小时二") == decided(
        "ignore", "hour_rolled"
    )
    assert harness.rng.calls == [12, 12]
    assert [m.text for m in harness.store.buffer(GROUP)] == [
        "开场白",
        "同一小时",
        "冷却中",
        "冷却末",
        "冷却结束",
        "同一小时二",
    ]


async def test_cooldown_and_hour_marker_survive_restart(tmp_path: Path) -> None:
    harness = Harness(tmp_path / "s.sqlite", rolls=(0,))
    assert await harness.send(at(hour=9), "开场") == decided("proactive", "roll_hit")
    harness.reopen()
    assert await harness.send(at(hour=9, minute=10), "同一小时") == decided("ignore", "cooldown")
    assert await harness.send(at(hour=10), "冷却") == decided("ignore", "cooldown")
    assert harness.rng.calls == [12]


async def test_one_roll_per_clock_hour_on_misses(harness: Harness) -> None:
    assert await harness.send(at(hour=10), "a") == decided("ignore", "roll_miss")
    assert await harness.send(at(hour=10, minute=30), "b") == decided("ignore", "hour_rolled")
    assert await harness.send(at(hour=11), "c") == decided("ignore", "roll_miss")
    assert harness.rng.calls == [12, 12]


async def test_roll_hit_is_still_buffered_as_context(harness: Harness) -> None:
    harness.rng = ScriptedRng((0,))
    harness.gate = harness._build()
    assert await harness.send(at(hour=9), "开场") == decided("proactive", "roll_hit")
    assert [m.text for m in harness.gate.recent_context(GROUP)] == ["开场"]


async def test_buffer_keeps_last_30_unhandled_messages(harness: Harness) -> None:
    for i in range(35):
        await harness.send(at(hour=8, minute=i), f"m{i}")
    texts = [m.text for m in harness.gate.recent_context(GROUP)]
    assert texts == [f"m{i}" for i in range(5, 35)]


async def test_buffer_custom_capacity(tmp_path: Path) -> None:
    harness = Harness(tmp_path / "s.sqlite", policy=GatePolicy(buffer_capacity=3))
    for i in range(5):
        await harness.send(at(hour=8, minute=i), f"m{i}")
    assert [m.text for m in harness.gate.recent_context(GROUP)] == ["m2", "m3", "m4"]


# --- follow-up ----------------------------------------------------------------------------


async def test_followup_extends_on_each_member_reply(harness: Harness) -> None:
    harness.gate.note_bot_spoke(GROUP, at(hour=10), message_id="B1")
    assert await harness.send(at(hour=10, minute=2, second=59), "接话") == decided(
        "followup", "member_reply"
    )
    assert await harness.send(at(hour=10, minute=5, second=58), "又接") == decided(
        "followup", "member_reply"
    )
    assert await harness.send(at(hour=10, minute=8, second=58), "太晚") == decided(
        "ignore", "roll_miss"
    )


@pytest.mark.parametrize(
    ("delay", "expected"),
    [(179, decided("followup", "member_reply")), (180, decided("ignore", "roll_miss"))],
)
async def test_followup_deadline_is_exact_three_minutes(
    harness: Harness, delay: int, expected: GateDecision
) -> None:
    harness.gate.note_bot_spoke(GROUP, at(hour=11), message_id="B1")
    assert await harness.send(at(hour=11, second=delay), "接") == expected


async def test_wake_inside_followup_is_wake(harness: Harness) -> None:
    harness.gate.note_bot_spoke(GROUP, at(hour=10))
    assert await harness.send(at(hour=10, minute=1), "大肥鱼", at_bot=True) == decided(
        "wake", "wake_keyword"
    )


async def test_followup_needs_a_prior_bot_message(harness: Harness) -> None:
    assert await harness.send(at(hour=10), "无人接话") == decided("ignore", "roll_miss")


async def test_note_other_reply_reports_open_window(harness: Harness) -> None:
    harness.gate.note_bot_spoke(GROUP, at(hour=10))
    assert harness.gate.note_other_reply(GROUP, at(hour=10, minute=1)) is True
    # Each reply extends the window by 3 minutes: 10:04:00, then 10:06:01 (closed at deadline).
    assert harness.gate.note_other_reply(GROUP, at(hour=10, minute=3, second=1)) is True
    assert harness.gate.note_other_reply(GROUP, at(hour=10, minute=6, second=1)) is False


async def test_followup_is_not_extended_during_sleep(harness: Harness) -> None:
    harness.gate.note_bot_spoke(GROUP, at(hour=23, minute=59))
    assert await harness.send(at(day=1, hour=0, minute=1), "夜里") == decided("ignore", "sleep")
    assert harness.gate.note_other_reply(GROUP, at(day=1, hour=0, minute=1)) is False


# --- timezone and policy ------------------------------------------------------------------


async def test_default_timezone_uses_system_local_hours(tmp_path: Path) -> None:
    harness = Harness(tmp_path / "s.sqlite", tz=None)
    at_three = datetime(2026, 10, 30, 3, 0).timestamp()
    at_nine = datetime(2026, 10, 30, 9, 0).timestamp()
    assert await harness.send(at_three, "夜") == decided("ignore", "sleep")
    assert await harness.send(at_nine, "早") == decided("ignore", "roll_miss")


def test_policy_rejects_invalid_values() -> None:
    with pytest.raises(ValueError):
        GatePolicy(sleep_end_hour=25)
    with pytest.raises(ValueError):
        GatePolicy(proactive_start_hour=20, proactive_end_hour=8)
    with pytest.raises(ValueError):
        GatePolicy(proactive_one_in=0)
    with pytest.raises(ValueError):
        GatePolicy(mute_duration=0)
    with pytest.raises(ValueError):
        GatePolicy(followup_window=0)
    with pytest.raises(ValueError):
        GatePolicy(buffer_capacity=0)
