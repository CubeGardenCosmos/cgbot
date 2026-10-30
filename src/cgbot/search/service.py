"""Search entry point shared by the LLM tools and the bridge.

``SearchService.search`` never raises for an engine failure: each engine runs independently, a
failing engine is reported by code in ``SearchOutcome.engine_codes``, and only when every attempted
engine fails (or the arguments are invalid) is ``ok`` false.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Final

from cgbot.config.settings import SearchConfig
from cgbot.search.aggregate import merge_results
from cgbot.search.engines import EngineError, SearchEngine, make_engine
from cgbot.search.models import (
    E_ALL_FAILED,
    E_BAD_ARG,
    E_EMPTY_QUERY,
    E_ENGINE_DISABLED,
    E_PARSE,
    E_TIMEOUT,
    E_UNKNOWN_ENGINE,
    ENGINE_NAMES,
    FRESHNESS_VALUES,
    SearchOutcome,
    SearchResult,
)

log = logging.getLogger(__name__)

ENGINE_LABELS: Final[Mapping[str, str]] = {
    "bing": "必应",
    "baidu": "百度",
    "bilibili": "哔哩哔哩",
}
# Accepted spellings for ``engine``; the model is offered only the canonical names.
_ENGINE_ALIASES: Final[Mapping[str, str]] = {
    **{name: name for name in ENGINE_NAMES},
    **{label.lower(): name for name, label in ENGINE_LABELS.items()},
    "b站": "bilibili",
}
MAX_QUERY_CHARS: Final = 200


class SearchService:
    """Owns one engine instance per enabled engine (each with its own transport)."""

    def __init__(
        self,
        config: SearchConfig,
        *,
        engines: Mapping[str, SearchEngine] | None = None,
    ) -> None:
        self._config = config
        if engines is None:
            engines = {
                name: make_engine(
                    name,
                    timeout=config.timeout_seconds,
                    cookies=_cookies_of(config, name),
                )
                for name in config.engines
            }
        self._engines: dict[str, SearchEngine] = dict(engines)

    @property
    def config(self) -> SearchConfig:
        return self._config

    @property
    def engine_names(self) -> tuple[str, ...]:
        """Enabled engines in configured priority order."""
        return tuple(name for name in self._config.engines if name in self._engines)

    async def search(
        self,
        query: str,
        engine: str | None = None,
        count: int | None = None,
        freshness: str | None = None,
    ) -> SearchOutcome:
        text = (query or "").strip()
        if not text:
            return SearchOutcome(ok=False, code=E_EMPTY_QUERY)
        text = text[:MAX_QUERY_CHARS]

        period = (freshness or "").strip().lower()
        if period and period not in FRESHNESS_VALUES:
            return SearchOutcome(ok=False, code=E_BAD_ARG)

        targets, code = self._targets(engine)
        if code:
            return SearchOutcome(ok=False, code=code)

        limit = self._config.default_count if count is None else count
        limit = max(1, min(limit, self._config.max_count))

        runs = await asyncio.gather(*(self._run(name, text, limit, period) for name in targets))
        engine_codes = tuple((name, err) for name, _, err in runs if err)
        per_engine: list[list[SearchResult]] = [results for _, results, err in runs if not err]
        merged = merge_results(text, per_engine, limit)
        if merged:
            return SearchOutcome(
                ok=True,
                results=tuple(merged),
                engine_codes=engine_codes,
                attempted=tuple(targets),
            )
        return SearchOutcome(
            ok=False,
            code=E_ALL_FAILED,
            engine_codes=engine_codes,
            attempted=tuple(targets),
        )

    def _targets(self, engine: str | None) -> tuple[list[str], str]:
        requested = (engine or "").strip().lower()
        if not requested:
            default = self._config.default_engine
            if default and default in self._engines:
                return [default], ""
            return list(self.engine_names), ""
        name = _ENGINE_ALIASES.get(requested)
        if name is None:
            return [], E_UNKNOWN_ENGINE
        if name not in self._engines:
            return [], E_ENGINE_DISABLED
        return [name], ""

    async def _run(
        self,
        name: str,
        query: str,
        count: int,
        freshness: str,
    ) -> tuple[str, list[SearchResult], str]:
        # Warmup plus one request: give the pair twice the per-request budget.
        budget = self._config.timeout_seconds * 2
        try:
            results = await asyncio.wait_for(
                self._engines[name].search(query, count, freshness),
                timeout=budget,
            )
        except EngineError as exc:
            code = exc.code
        except TimeoutError:
            code = E_TIMEOUT
        except Exception as exc:  # one engine must not fail the others; log only the type
            log.warning("search engine=%s unexpected=%s", name, type(exc).__name__)
            code = E_PARSE
        else:
            return name, results, ""
        log.warning("search engine=%s code=%s", name, code)
        return name, [], code

    async def close(self) -> None:
        await asyncio.gather(*(e.close() for e in self._engines.values()), return_exceptions=True)


def _cookies_of(config: SearchConfig, name: str) -> str:
    if name == "baidu" and config.baidu_cookies is not None:
        return config.baidu_cookies.reveal()
    if name == "bilibili" and config.bilibili_cookies is not None:
        return config.bilibili_cookies.reveal()
    return ""
