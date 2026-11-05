from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from cgbot.logfmt import ZonedFormatter


def test_timestamps_use_configured_zone_not_process_tz(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    try:
        record = logging.LogRecord("cgbot.x", logging.INFO, __file__, 1, "hello", None, None)
        record.created = datetime(2026, 10, 29, 19, 0, 0, tzinfo=UTC).timestamp()
        record.msecs = 123.0
        line = ZonedFormatter(ZoneInfo("Asia/Kuala_Lumpur")).format(record)
    finally:
        monkeypatch.undo()
        time.tzset()
    assert line == "2026-10-30 03:00:00,123 INFO cgbot.x: hello"
