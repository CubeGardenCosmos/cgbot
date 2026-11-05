"""LLM-readable message timestamps, shared by the private and group render paths.

A stamp is the item's stored epoch time in the configured zone, minute precision, with a Chinese
weekday: ``[2026-11-05 09:41 周四]``. It depends only on the stored time and the zone, never on
the wall clock or the process ``TZ``, so a rendered history is byte-identical across renders and
the provider's prompt-cache prefix stays valid.
"""

from __future__ import annotations

from datetime import datetime, tzinfo
from zoneinfo import ZoneInfo

_WEEKDAYS = "一二三四五六日"


def format_stamp(ts: float, tz: tzinfo) -> str:
    """``[YYYY-MM-DD HH:MM 周X]`` for epoch seconds ``ts`` in ``tz``."""
    moment = datetime.fromtimestamp(ts, tz)
    return f"[{moment:%Y-%m-%d %H:%M} 周{_WEEKDAYS[moment.weekday()]}]"


def time_instruction(tz: ZoneInfo) -> str:
    """The one-line system-prompt note that explains the stamps."""
    return f"消息前的方括号时间为该消息的当地时间({tz.key}),最新一条即当前时间附近。"
