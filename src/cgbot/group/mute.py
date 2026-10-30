"""Mute rules (R3.2). A mute keyword only counts when the message addresses the bot."""

from __future__ import annotations

from collections.abc import Iterable

from cgbot.group.text import contains_keyword


def mute_until(now: float, duration: float) -> float:
    return now + duration


def is_muted(muted_until: float | None, now: float) -> bool:
    """Muted while ``now < muted_until``; the expiry instant itself is not muted."""
    return muted_until is not None and now < muted_until


def is_mute_trigger(addressed: bool, text: str, keywords: Iterable[str]) -> bool:
    return addressed and contains_keyword(text, keywords)
