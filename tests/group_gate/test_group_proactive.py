from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from cgbot.group.proactive import RollResult, evaluate_roll, followup_open, hour_key

TZ = timezone(timedelta(hours=8))
HOUR = 3600.0


class ScriptedRng:
    """Returns scripted values; an unexpected call exhausts the script and fails loudly."""

    def __init__(self, values: list[int]) -> None:
        self.values = list(values)
        self.calls: list[int] = []

    def randrange(self, stop: int, /) -> int:
        self.calls.append(stop)
        return self.values.pop(0)


def local_at(hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 10, 30, tzinfo=TZ) + timedelta(hours=hour, minutes=minute, seconds=second)


def roll(
    now_local: datetime,
    rng: ScriptedRng,
    *,
    last_roll_hour: str | None = None,
    last_proactive_at: float | None = None,
) -> RollResult:
    return evaluate_roll(
        now=now_local.timestamp(),
        local=now_local,
        last_roll_hour=last_roll_hour,
        last_proactive_at=last_proactive_at,
        rng=rng,
        start_hour=8,
        end_hour=24,
        one_in=12,
        cooldown=12 * HOUR,
    )


@pytest.mark.parametrize(("hour", "minute", "second"), [(0, 0, 0), (3, 30, 0), (7, 59, 59)])
def test_outside_window_never_rolls(hour: int, minute: int, second: int) -> None:
    rng = ScriptedRng([])
    result = roll(local_at(hour, minute, second), rng)
    assert result.reason == "outside_window"
    assert result.hit is False
    assert result.roll_hour is None
    assert rng.calls == []


@pytest.mark.parametrize(("hour", "minute", "second"), [(8, 0, 0), (23, 59, 59)])
def test_window_start_and_last_second_are_inside(hour: int, minute: int, second: int) -> None:
    rng = ScriptedRng([5])
    result = roll(local_at(hour, minute, second), rng)
    assert result.reason == "roll_miss"
    assert rng.calls == [12]


def test_hit_on_zero_draw_records_hour_and_start_of_cooldown() -> None:
    rng = ScriptedRng([0])
    at = local_at(9, 5)
    result = roll(at, rng)
    assert result.reason == "roll_hit"
    assert result.hit is True
    assert result.roll_hour == "2026-10-30T09"
    assert result.proactive_at == at.timestamp()
    assert rng.calls == [12]


def test_miss_consumes_hour_without_starting_cooldown() -> None:
    result = roll(local_at(10), ScriptedRng([1]))
    assert result.reason == "roll_miss"
    assert result.hit is False
    assert result.roll_hour == "2026-10-30T10"
    assert result.proactive_at is None


def test_same_hour_is_rolled_once_without_drawing() -> None:
    rng = ScriptedRng([])
    result = roll(local_at(9, 30), rng, last_roll_hour="2026-10-30T09")
    assert result.reason == "hour_rolled"
    assert result.roll_hour is None
    assert rng.calls == []


def test_cooldown_blocks_until_exactly_twelve_hours_after_hit() -> None:
    hit_at = local_at(9, 0).timestamp()
    blocked_rng = ScriptedRng([])
    blocked = roll(
        local_at(20, 59, 59),
        blocked_rng,
        last_proactive_at=hit_at,
        last_roll_hour="2026-10-30T09",
    )
    assert blocked.reason == "cooldown"
    assert blocked_rng.calls == []

    allowed_rng = ScriptedRng([7])
    allowed = roll(
        local_at(21, 0, 0),
        allowed_rng,
        last_proactive_at=hit_at,
        last_roll_hour="2026-10-30T09",
    )
    assert allowed.reason == "roll_miss"
    assert allowed.roll_hour == "2026-10-30T21"
    assert allowed_rng.calls == [12]


def test_cooldown_skip_does_not_consume_the_hour() -> None:
    hit_at = local_at(9, 0).timestamp()
    skipped = roll(local_at(10, 0), ScriptedRng([]), last_proactive_at=hit_at)
    assert skipped.reason == "cooldown"
    assert skipped.roll_hour is None


def test_seeded_rng_is_reproducible() -> None:
    first = random.Random(42)
    second = random.Random(42)
    assert [first.randrange(12) for _ in range(20)] == [second.randrange(12) for _ in range(20)]


def test_hour_key_is_local_hour_stamp() -> None:
    assert hour_key(local_at(8, 59, 59)) == "2026-10-30T08"


def test_followup_open_is_closed_at_deadline() -> None:
    assert followup_open(None, 0.0) is False
    assert followup_open(100.0, 99.999) is True
    assert followup_open(100.0, 100.0) is False
