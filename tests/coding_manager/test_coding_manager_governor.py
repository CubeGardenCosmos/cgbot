from __future__ import annotations

from dataclasses import replace

import pytest
from manager_fakes import CONFIG, FakeClock

from cgbot.coding.governor import ConcurrencyGovernor


@pytest.mark.parametrize(
    "change",
    [
        {"per_user": 0},
        {"degraded": 0},
        {"degraded": 5},
        {"degrade_failures": 0},
        {"degrade_window_seconds": 0.0},
        {"recover_after_seconds": float("nan")},
    ],
)
def test_invalid_config_is_rejected(change: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        ConcurrencyGovernor(replace(CONFIG, **change))


def test_three_failures_inside_window_degrade_all_users() -> None:
    clock = FakeClock(0.0)
    seen: list[int] = []
    governor = ConcurrencyGovernor(CONFIG, clock.time, on_change=seen.append)
    for at in (0.0, 10.0, 59.0):
        clock.now = at
        governor.on_upstream_status(503)
    assert governor.degraded
    assert governor.limit() == CONFIG.degraded
    assert seen == [CONFIG.degraded]


def test_failures_spread_beyond_window_do_not_degrade() -> None:
    clock = FakeClock(0.0)
    governor = ConcurrencyGovernor(CONFIG, clock.time)
    for at in (0.0, 30.0, 61.0):
        clock.now = at
        governor.on_upstream_status(0)
    assert not governor.degraded
    assert governor.limit() == CONFIG.per_user


def test_only_429_5xx_and_connection_failures_count() -> None:
    clock = FakeClock(0.0)
    governor = ConcurrencyGovernor(CONFIG, clock.time)
    for _ in range(5):
        governor.on_upstream_status(200)
        governor.on_upstream_status(404)
        governor.on_upstream_status(400)
    assert not governor.degraded
    governor.on_upstream_status(429)
    governor.on_upstream_status(500)
    governor.on_upstream_status(0)
    assert governor.degraded


def test_quiet_hour_restores_limit_and_notifies_listeners() -> None:
    clock = FakeClock(0.0)
    seen: list[int] = []
    governor = ConcurrencyGovernor(CONFIG, clock.time)
    governor.add_listener(seen.append)
    for _ in range(3):
        governor.on_upstream_status(429)
    assert governor.recovers_at() == pytest.approx(CONFIG.recover_after_seconds)
    clock.now = 3599.0
    assert governor.limit() == CONFIG.degraded
    clock.now = 3600.0
    assert governor.limit() == CONFIG.per_user
    assert not governor.degraded
    assert seen == [CONFIG.degraded, CONFIG.per_user]


def test_rate_limit_postpones_recovery() -> None:
    clock = FakeClock(0.0)
    governor = ConcurrencyGovernor(CONFIG, clock.time)
    for _ in range(3):
        governor.on_upstream_status(429)
    clock.now = 3000.0
    governor.on_upstream_status(429)
    assert governor.recovers_at() == pytest.approx(6600.0)
    clock.now = 6599.0
    assert governor.limit() == CONFIG.degraded
    clock.now = 6600.0
    assert governor.limit() == CONFIG.per_user


def test_non_429_failures_do_not_postpone_recovery() -> None:
    clock = FakeClock(0.0)
    governor = ConcurrencyGovernor(CONFIG, clock.time)
    for _ in range(3):
        governor.on_upstream_status(0)
    clock.now = 3000.0
    governor.on_upstream_status(503)
    assert governor.recovers_at() == pytest.approx(3600.0)


def test_recovers_at_is_none_when_not_degraded() -> None:
    governor = ConcurrencyGovernor(CONFIG, FakeClock(0.0).time)
    assert governor.recovers_at() is None
