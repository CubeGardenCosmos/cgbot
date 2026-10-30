"""Engine-neutral search result and outcome, plus the stable error codes.

Error codes are the only failure detail that leaves this package: logs and model-facing text
carry codes, never remote response bodies, cookies, or the query's user identity.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from cgbot.config.settings import SEARCH_ENGINE_NAMES

E_EMPTY_QUERY: Final = "E_EMPTY_QUERY"  # query is blank
E_BAD_ARG: Final = "E_BAD_ARG"  # count / freshness outside the accepted values
E_UNKNOWN_ENGINE: Final = "E_UNKNOWN_ENGINE"  # engine name not recognised
E_ENGINE_DISABLED: Final = "E_ENGINE_DISABLED"  # engine known but not enabled in [search]
E_TIMEOUT: Final = "E_TIMEOUT"  # request exceeded timeout_seconds
E_NETWORK: Final = "E_NETWORK"  # transport error
E_HTTP: Final = "E_HTTP"  # non-200 response
E_PARSE: Final = "E_PARSE"  # body could not be parsed
E_NO_RESULT: Final = "E_NO_RESULT"  # parsed fine, zero results
E_BAIDU_BLOCKED: Final = "E_BAIDU_BLOCKED"  # Baidu security check or empty result page
E_BILI_RISK: Final = "E_BILI_RISK"  # Bilibili risk control (HTTP 412 or API -412/-352)
E_ALL_FAILED: Final = "E_ALL_FAILED"  # every attempted engine failed

ENGINE_NAMES: Final = SEARCH_ENGINE_NAMES
FRESHNESS_VALUES: Final = ("day", "week", "month", "year")


@dataclass(frozen=True, slots=True)
class SearchResult:
    """One normalised hit. ``meta`` is an optional one-line extra (e.g. Bilibili UP主/播放量)."""

    title: str
    url: str
    snippet: str
    source: str  # engine name, one of ENGINE_NAMES
    meta: str = ""


@dataclass(frozen=True, slots=True)
class SearchOutcome:
    """Result of one search call.

    ``ok`` is true when at least one result exists (partial engine failures are listed in
    ``engine_codes``). When ``ok`` is false, ``code`` is the stable reason and ``engine_codes``
    carries per-engine codes. ``attempted`` lists the engines that were queried.
    """

    ok: bool
    results: tuple[SearchResult, ...] = ()
    code: str = ""
    engine_codes: tuple[tuple[str, str], ...] = ()
    attempted: tuple[str, ...] = ()
