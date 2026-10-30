"""web_search tool: schema, argument checks, model-facing text, gemini injection (offline)."""

from __future__ import annotations

from typing import Any, cast

from search_fakes import FakeEngine, hit

from cgbot.agent.tools import ToolContext, ToolResult, ToolSpec
from cgbot.config.settings import SearchConfig, SearchGeminiConfig
from cgbot.search import SearchService, search_tools
from cgbot.search.engines import EngineError
from cgbot.search.models import E_BAIDU_BLOCKED, E_HTTP, SearchOutcome
from cgbot.search.tools import format_outcome


def _config(**overrides: object) -> SearchConfig:
    base: dict[str, object] = {
        "engines": ("bing", "baidu"),
        "timeout_seconds": 5.0,
        "default_count": 5,
        "max_count": 10,
    }
    base.update(overrides)
    return SearchConfig(**base)  # type: ignore[arg-type]


def _service(
    bing: FakeEngine | None = None,
    baidu: FakeEngine | None = None,
    **config: object,
) -> SearchService:
    engines: dict[str, Any] = {}
    if bing is not None:
        engines["bing"] = bing
    if baidu is not None:
        engines["baidu"] = baidu
    return SearchService(_config(**config), engines=engines)


def _web_search(specs: list[ToolSpec]) -> ToolSpec:
    return next(spec for spec in specs if spec.name == "web_search")


async def _call(spec: ToolSpec, args: dict[str, Any]) -> ToolResult:
    return await spec.handler(cast(ToolContext, None), args)


def test_schema_lists_only_enabled_engines_and_bounds_count() -> None:
    spec = _web_search(search_tools(_service(bing=FakeEngine("bing"))))
    props = spec.parameters_schema["properties"]
    assert props["engine"]["enum"] == ["bing"]
    assert props["count"]["maximum"] == 10
    assert props["freshness"]["enum"] == ["day", "week", "month", "year"]
    assert spec.parameters_schema["required"] == ["query"]


async def test_success_output_is_numbered_and_labelled() -> None:
    bing = FakeEngine("bing", [hit("rust guide", "https://a.example/1", "snip one", source="bing")])
    spec = _web_search(search_tools(_service(bing=bing)))
    result = await _call(spec, {"query": "rust", "count": 3, "freshness": "week"})
    assert not result.is_error
    assert "1. rust guide" in result.output
    assert "链接：https://a.example/1" in result.output
    assert "摘要：snip one" in result.output
    assert bing.queries == [("rust", 3, "week")]


async def test_invalid_arguments_are_refused_without_searching() -> None:
    bing = FakeEngine("bing", [hit("x", "https://a.example/1")])
    spec = _web_search(search_tools(_service(bing=bing)))
    bad_args: list[dict[str, Any]] = [
        {},
        {"query": "   "},
        {"query": "x" * 201},
        {"query": "x", "count": 11},
        {"query": "x", "count": 0},
        {"query": "x", "count": True},
        {"query": "x", "freshness": "century"},
        {"query": "x", "engine": 7},
        {"query": "x", "unexpected": 1},
    ]
    for args in bad_args:
        result = await _call(spec, args)
        assert result.is_error, args
    assert bing.queries == []


async def test_unknown_engine_is_an_error_result() -> None:
    spec = _web_search(search_tools(_service(bing=FakeEngine("bing"))))
    result = await _call(spec, {"query": "x", "engine": "google"})
    assert result.is_error


async def test_all_engines_failing_is_an_error_result_with_text() -> None:
    bing = FakeEngine("bing", error=EngineError(E_HTTP))
    baidu = FakeEngine("baidu", error=EngineError(E_BAIDU_BLOCKED))
    spec = _web_search(search_tools(_service(bing=bing, baidu=baidu)))
    result = await _call(spec, {"query": "x"})
    assert result.is_error
    assert result.output


def test_format_outcome_reports_partial_engine_failures() -> None:
    outcome = SearchOutcome(
        ok=True,
        results=(hit("rust", "https://a.example/1", "s", source="bing"),),
        engine_codes=(("baidu", E_BAIDU_BLOCKED),),
        attempted=("bing", "baidu"),
    )
    text = format_outcome("rust", outcome)
    assert "rust" in text
    assert "百度" in text


def test_gemini_tool_is_appended_only_when_enabled() -> None:
    calls: list[SearchGeminiConfig] = []

    def factory(cfg: SearchGeminiConfig) -> ToolSpec:
        calls.append(cfg)
        return ToolSpec(
            name="gemini_search",
            description="gemini",
            parameters_schema={"type": "object", "properties": {}},
            handler=_gemini_noop,
        )

    disabled = search_tools(_service(bing=FakeEngine("bing")), gemini_factory=factory)
    assert [spec.name for spec in disabled] == ["web_search"]
    assert calls == []

    gemini = SearchGeminiConfig(enabled=True, endpoint="https://relay.example/search", token=None)
    enabled = search_tools(_service(bing=FakeEngine("bing"), gemini=gemini), gemini_factory=factory)
    assert [spec.name for spec in enabled] == ["web_search", "gemini_search"]
    assert calls == [gemini]


async def _gemini_noop(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    return ToolResult(output="")
