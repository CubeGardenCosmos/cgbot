"""Proactive opener roll and follow-up window rules (R3.2). Pure; rng and time are injected."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol


class Rng(Protocol):
    def randrange(self, stop: int, /) -> int: ...


RollReason = Literal["roll_hit", "roll_miss", "cooldown", "hour_rolled", "outside_window"]


@dataclass(frozen=True, slots=True)
class RollResult:
    reason: RollReason
    hit: bool
    # Hour key to persist as consumed; None when no roll happened.
    roll_hour: str | None
    # Set to ``now`` on a hit (starts the cooldown); None otherwise.
    proactive_at: float | None


def hour_key(local: datetime) -> str:
    return local.strftime("%Y-%m-%dT%H")


def evaluate_roll(
    *,
    now: float,
    local: datetime,
    last_roll_hour: str | None,
    last_proactive_at: float | None,
    rng: Rng,
    start_hour: int,
    end_hour: int,
    one_in: int,
    cooldown: float,
) -> RollResult:
    """Roll once per local clock hour inside ``[start_hour, end_hour)``.

    A cooldown skip does not consume the hour, so the next eligible message after the
    cooldown in the same hour can still roll.
    """
    if not start_hour <= local.hour < end_hour:
        return RollResult("outside_window", False, None, None)
    if last_proactive_at is not None and now < last_proactive_at + cooldown:
        return RollResult("cooldown", False, None, None)
    key = hour_key(local)
    if last_roll_hour == key:
        return RollResult("hour_rolled", False, None, None)
    hit = rng.randrange(one_in) == 0
    if hit:
        return RollResult("roll_hit", True, key, now)
    return RollResult("roll_miss", False, key, None)


def followup_open(until: float | None, now: float) -> bool:
    """Follow-up is open while ``now < until``; the deadline instant is closed."""
    return until is not None and now < until
