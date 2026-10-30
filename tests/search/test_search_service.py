"""SearchService: targeting, clamping, merge, partial failure, and error codes (offline)."""

from __future__ import annotations

from search_fakes import FakeEngine, hit

from cgbot.config.settings import SearchConfig
from cgbot.search import SearchService
from cgbot.search.engines import EngineError
from cgbot.search.models import (
    E_ALL_FAILED,
    E_BAD_ARG,
    E_BAIDU_BLOCKED,
    E_EMPTY_QUERY,
    E_ENGINE_DISABLED,
    E_HTTP,
    E_PARSE,
    E_TIMEOUT,
    E_UNKNOWN_ENGINE,
)


def _config(**overrides: object) -> SearchConfig:
    base: dict[str, object] = {
        "engines": ("bing", "baidu"),
        "default_engine": "",
        "timeout_seconds": 5.0,
        "default_count": 5,
        "max_count": 10,
    }
    base.update(overrides)
    return SearchConfig(**base)  # type: ignore[arg-type]


def _engines(**engines: FakeEngine) -> dict[str, FakeEngine]:
    return dict(engines)


async def test_merges_results_from_all_enabled_engines() -> None:
    bing = FakeEngine("bing", [hit("rust guide", "https://a.example/1", source="bing")])
    baidu = FakeEngine("baidu", [hit("rust tips", "https://a.example/2", source="baidu")])
    service = SearchService(_config(), engines=_engines(bing=bing, baidu=baidu))
    outcome = await service.search("rust guide")
    assert outcome.ok
    assert {r.url for r in outcome.results} == {"https://a.example/1", "https://a.example/2"}
    assert outcome.attempted == ("bing", "baidu")
    assert outcome.engine_codes == ()


async def test_one_failing_engine_does_not_fail_the_others() -> None:
    bing = FakeEngine("bing", error=EngineError(E_HTTP))
    baidu = FakeEngine("baidu", [hit("rust", "https://a.example/2", source="baidu")])
    service = SearchService(_config(), engines=_engines(bing=bing, baidu=baidu))
    outcome = await service.search("rust")
    assert outcome.ok
    assert [r.url for r in outcome.results] == ["https://a.example/2"]
    assert outcome.engine_codes == (("bing", E_HTTP),)


async def test_all_engines_failing_reports_all_failed_with_codes() -> None:
    bing = FakeEngine("bing", error=EngineError(E_HTTP))
    baidu = FakeEngine("baidu", error=EngineError(E_BAIDU_BLOCKED))
    service = SearchService(_config(), engines=_engines(bing=bing, baidu=baidu))
    outcome = await service.search("rust")
    assert not outcome.ok
    assert outcome.code == E_ALL_FAILED
    assert outcome.engine_codes == (("bing", E_HTTP), ("baidu", E_BAIDU_BLOCKED))


async def test_unexpected_engine_exception_maps_to_parse_code() -> None:
    bing = FakeEngine("bing", error=RuntimeError("boom"))
    service = SearchService(_config(engines=("bing",)), engines=_engines(bing=bing))
    outcome = await service.search("rust")
    assert outcome.code == E_ALL_FAILED
    assert outcome.engine_codes == (("bing", E_PARSE),)


async def test_engine_exceeding_budget_maps_to_timeout() -> None:
    bing = FakeEngine("bing", [hit("rust", "https://a.example/1")], delay=1.0)
    service = SearchService(
        _config(engines=("bing",), timeout_seconds=0.01), engines=_engines(bing=bing)
    )
    outcome = await service.search("rust")
    assert outcome.code == E_ALL_FAILED
    assert outcome.engine_codes == (("bing", E_TIMEOUT),)


async def test_blank_query_is_rejected_before_any_engine_runs() -> None:
    bing = FakeEngine("bing", [hit("x", "https://a.example/1")])
    service = SearchService(_config(engines=("bing",)), engines=_engines(bing=bing))
    outcome = await service.search("   ")
    assert outcome.code == E_EMPTY_QUERY
    assert bing.queries == []


async def test_bad_freshness_is_rejected() -> None:
    bing = FakeEngine("bing", [hit("x", "https://a.example/1")])
    service = SearchService(_config(engines=("bing",)), engines=_engines(bing=bing))
    outcome = await service.search("x", freshness="century")
    assert outcome.code == E_BAD_ARG
    assert bing.queries == []


async def test_unknown_and_disabled_engines_are_distinct_codes() -> None:
    bing = FakeEngine("bing", [hit("x", "https://a.example/1")])
    service = SearchService(_config(engines=("bing",)), engines=_engines(bing=bing))
    assert (await service.search("x", engine="google")).code == E_UNKNOWN_ENGINE
    assert (await service.search("x", engine="baidu")).code == E_ENGINE_DISABLED


async def test_chinese_alias_targets_single_engine() -> None:
    bing = FakeEngine("bing", [hit("x", "https://a.example/1")])
    baidu = FakeEngine("baidu", [hit("x", "https://a.example/2")])
    service = SearchService(_config(), engines=_engines(bing=bing, baidu=baidu))
    outcome = await service.search("x", engine="必应")
    assert outcome.attempted == ("bing",)
    assert baidu.queries == []


async def test_default_engine_restricts_unqualified_searches() -> None:
    bing = FakeEngine("bing", [hit("x", "https://a.example/1")])
    baidu = FakeEngine("baidu", [hit("x", "https://a.example/2")])
    service = SearchService(
        _config(default_engine="bing"), engines=_engines(bing=bing, baidu=baidu)
    )
    outcome = await service.search("x")
    assert outcome.attempted == ("bing",)
    assert baidu.queries == []


async def test_count_is_clamped_and_defaults_apply() -> None:
    bing = FakeEngine("bing", [hit("x", "https://a.example/1")])
    service = SearchService(_config(engines=("bing",)), engines=_engines(bing=bing))
    await service.search("x")
    await service.search("x", count=999)
    await service.search("x", count=0)
    assert [q[1] for q in bing.queries] == [5, 10, 1]


async def test_freshness_is_normalised_and_forwarded() -> None:
    bing = FakeEngine("bing", [hit("x", "https://a.example/1")])
    service = SearchService(_config(engines=("bing",)), engines=_engines(bing=bing))
    await service.search("x", freshness="Week")
    assert bing.queries[0][2] == "week"


async def test_long_query_is_truncated() -> None:
    bing = FakeEngine("bing", [hit("x", "https://a.example/1")])
    service = SearchService(_config(engines=("bing",)), engines=_engines(bing=bing))
    await service.search("字" * 300)
    assert len(bing.queries[0][0]) == 200


async def test_engine_names_follow_configured_priority() -> None:
    service = SearchService(
        _config(engines=("baidu", "bing")),
        engines=_engines(bing=FakeEngine("bing"), baidu=FakeEngine("baidu")),
    )
    assert service.engine_names == ("baidu", "bing")


async def test_close_closes_every_engine() -> None:
    bing = FakeEngine("bing")
    baidu = FakeEngine("baidu")
    service = SearchService(_config(), engines=_engines(bing=bing, baidu=baidu))
    await service.close()
    assert bing.closed and baidu.closed
