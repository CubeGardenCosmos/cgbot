"""Purpose -> reasoning effort lookup (R2.4).

Every call site asks for its effort through ``effort_for`` instead of hard-coding a value.
The configured string is returned verbatim; it is passed to the upstream unchanged, so an
unsupported value surfaces as the upstream's own error rather than a silent substitution.
"""

from __future__ import annotations

from typing import Literal

from cgbot.config.settings import ReasoningTiers

Purpose = Literal["summary", "private", "planning", "group"]


def effort_for(purpose: Purpose, tiers: ReasoningTiers) -> str:
    """Configured reasoning effort for ``purpose``. Unknown purposes raise ``ValueError``."""
    values = {
        "summary": tiers.summary,
        "private": tiers.private,
        "planning": tiers.planning,
        "group": tiers.group,
    }
    if purpose not in values:
        raise ValueError(f"unknown reasoning purpose: {purpose!r}")
    return values[purpose]
