"""Log formatter that stamps records in an explicit zone, independent of the process TZ."""

from __future__ import annotations

import logging
from datetime import datetime, tzinfo

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


class ZonedFormatter(logging.Formatter):
    """``asctime`` is rendered in ``tz`` as ``YYYY-MM-DD HH:MM:SS,mmm`` (stdlib default layout)."""

    def __init__(self, tz: tzinfo) -> None:
        super().__init__(LOG_FORMAT)
        self._tz = tz

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:  # noqa: N802
        moment = datetime.fromtimestamp(record.created, self._tz)
        if datefmt:
            return moment.strftime(datefmt)
        return f"{moment:%Y-%m-%d %H:%M:%S},{int(record.msecs):03d}"
