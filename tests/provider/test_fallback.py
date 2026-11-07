"""FallbackProvider 与 build_provider 的降级装配:顺序、错误、流式、生命周期(全部离线)。"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import pytest

from cgbot.config import ConfigError, ProviderConfig, ReasoningTiers, Secret
from cgbot.config.settings import FallbackModelConfig
from cgbot.provider import (
    BadRequest,
    ChatCompletionsProvider,
    Completed,
    FallbackProvider,
    GeminiProvider,
    ModelRequest,
    ModelResponse,
    ProviderError,
    RateLimited,
    ResponsesProvider,
    StreamEvent,
    TextDelta,
    UpstreamUnavailable,
    Usage,
    build_provider,
)

REQUEST = ModelRequest(instructions="i", input=[])


def response_of(response_id: str) -> ModelResponse:
    return ModelResponse(items=(), usage=Usage(), response_id=response_id, status="completed")


class FakeProvider:
    """Scripted member: each call pops the next outcome (an exception is raised, else returned).

    A stream script is a list of events, each an event to yield or an exception to raise.
    """

    def __init__(
        self,
        name: str,
        *,
        outcome: ProviderError | None = None,
        stream: list[StreamEvent | ProviderError] | None = None,
        context_window: int = 1000,
    ) -> None:
        self._name = name
        self._outcome = outcome
        self._stream = stream
        self._context_window = context_window
        self.calls = 0
        self.closed = 0
        self.stream_closed = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def context_window(self) -> int:
        return self._context_window

    async def respond(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        if self._outcome is not None:
            raise self._outcome
        return response_of(self._name)

    async def respond_stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        self.calls += 1
        try:
            if self._stream is None:
                if self._outcome is not None:
                    raise self._outcome
                yield TextDelta(self._name)
                yield Completed(response_of(self._name))
                return
            for step in self._stream:
                if isinstance(step, ProviderError):
                    raise step
                yield step
        finally:
            self.stream_closed += 1

    async def aclose(self) -> None:
        self.closed += 1


def rate_limited() -> RateLimited:
    return RateLimited("slow down", retry_after=None)


async def collect(provider: FallbackProvider) -> list[StreamEvent]:
    return [event async for event in provider.respond_stream(REQUEST)]


def test_requires_at_least_one_member() -> None:
    with pytest.raises(ValueError, match="at least one"):
        FallbackProvider([])


def test_name_and_context_window_are_the_primarys() -> None:
    primary = FakeProvider("p", context_window=111)
    provider = FallbackProvider([primary, FakeProvider("f", context_window=999)])

    assert provider.name == "p"
    assert provider.context_window == 111


async def test_primary_success_does_not_touch_fallbacks() -> None:
    primary, fallback = FakeProvider("p"), FakeProvider("f")

    result = await FallbackProvider([primary, fallback]).respond(REQUEST)

    assert result.response_id == "p"
    assert (primary.calls, fallback.calls) == (1, 0)


@pytest.mark.parametrize(
    "error",
    [rate_limited(), UpstreamUnavailable("boom"), BadRequest("nope", status=400)],
    ids=["rate_limited", "unavailable", "bad_request"],
)
async def test_falls_through_on_each_provider_error(error: ProviderError) -> None:
    primary = FakeProvider("p", outcome=error)
    second = FakeProvider("s")

    result = await FallbackProvider([primary, second]).respond(REQUEST)

    assert result.response_id == "s"
    assert (primary.calls, second.calls) == (1, 1)


async def test_tries_members_in_order_until_one_answers() -> None:
    members = [
        FakeProvider("a", outcome=rate_limited()),
        FakeProvider("b", outcome=UpstreamUnavailable("down")),
        FakeProvider("c"),
        FakeProvider("d"),
    ]

    result = await FallbackProvider(members).respond(REQUEST)

    assert result.response_id == "c"
    assert [m.calls for m in members] == [1, 1, 1, 0]


async def test_all_failing_raises_the_last_error() -> None:
    last = UpstreamUnavailable("last one")
    members = [
        FakeProvider("a", outcome=rate_limited()),
        FakeProvider("b", outcome=BadRequest("second", status=400)),
        FakeProvider("c", outcome=last),
    ]

    with pytest.raises(UpstreamUnavailable) as info:
        await FallbackProvider(members).respond(REQUEST)

    assert info.value is last
    assert [m.calls for m in members] == [1, 1, 1]


async def test_non_provider_errors_are_not_swallowed() -> None:
    class Broken(FakeProvider):
        async def respond(self, request: ModelRequest) -> ModelResponse:
            raise RuntimeError("bug")

    second = FakeProvider("s")
    with pytest.raises(RuntimeError, match="bug"):
        await FallbackProvider([Broken("p"), second]).respond(REQUEST)
    assert second.calls == 0


async def test_every_turn_restarts_at_the_primary() -> None:
    primary = FakeProvider("p", outcome=rate_limited())
    second = FakeProvider("s")
    provider = FallbackProvider([primary, second])

    await provider.respond(REQUEST)
    await provider.respond(REQUEST)
    await collect(provider)

    assert (primary.calls, second.calls) == (3, 3)


async def test_warning_per_failure_and_info_when_fallback_answers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider = FallbackProvider(
        [FakeProvider("p-model", outcome=rate_limited()), FakeProvider("s-model")]
    )

    with caplog.at_level(logging.INFO, logger="cgbot.provider.fallback"):
        await provider.respond(REQUEST)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(warnings) == 1
    assert "p-model" in warnings[0].getMessage()
    assert "RateLimited" in warnings[0].getMessage()
    assert len(infos) == 1
    assert "s-model" in infos[0].getMessage()


async def test_primary_answer_logs_nothing_at_info(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="cgbot.provider.fallback"):
        await FallbackProvider([FakeProvider("p"), FakeProvider("s")]).respond(REQUEST)

    assert caplog.records == []


# --- streaming ---------------------------------------------------------------------------------


async def test_stream_primary_success() -> None:
    primary, fallback = FakeProvider("p"), FakeProvider("f")

    events = await collect(FallbackProvider([primary, fallback]))

    assert events[0] == TextDelta("p")
    assert isinstance(events[-1], Completed)
    assert fallback.calls == 0


async def test_stream_falls_back_when_failure_precedes_any_event() -> None:
    primary = FakeProvider("p", outcome=UpstreamUnavailable("connect"))
    second = FakeProvider("s")

    events = await collect(FallbackProvider([primary, second]))

    assert events[0] == TextDelta("s")
    assert (primary.calls, second.calls) == (1, 1)
    assert primary.stream_closed == 1  # failed member's stream is finalized


async def test_stream_does_not_fall_back_after_first_event() -> None:
    primary = FakeProvider("p", stream=[TextDelta("partial"), UpstreamUnavailable("cut off")])
    second = FakeProvider("s")
    seen: list[StreamEvent] = []

    with pytest.raises(UpstreamUnavailable, match="cut off"):
        async for event in FallbackProvider([primary, second]).respond_stream(REQUEST):
            seen.append(event)

    assert seen == [TextDelta("partial")]
    assert second.calls == 0


async def test_stream_all_fail_raises_last_error() -> None:
    last = BadRequest("final", status=400)
    members = [
        FakeProvider("a", outcome=rate_limited()),
        FakeProvider("b", outcome=last),
    ]

    with pytest.raises(BadRequest) as info:
        await collect(FallbackProvider(members))

    assert info.value is last


async def test_stream_member_failing_mid_fallback_is_not_retried_elsewhere() -> None:
    members = [
        FakeProvider("a", outcome=rate_limited()),
        FakeProvider("b", stream=[TextDelta("x"), UpstreamUnavailable("cut")]),
        FakeProvider("c"),
    ]

    with pytest.raises(UpstreamUnavailable, match="cut"):
        await collect(FallbackProvider(members))

    assert [m.calls for m in members] == [1, 1, 0]


async def test_stream_abandoned_early_closes_member_stream() -> None:
    primary = FakeProvider("p")
    stream = FallbackProvider([primary]).respond_stream(REQUEST)

    first = await anext(stream)
    await stream.aclose()  # type: ignore[attr-defined]

    assert first == TextDelta("p")
    assert primary.stream_closed == 1


# --- lifecycle ---------------------------------------------------------------------------------


async def test_aclose_closes_every_member_and_raises_first_error() -> None:
    class Failing(FakeProvider):
        async def aclose(self) -> None:
            await super().aclose()
            raise RuntimeError("close failed")

    first, second = Failing("a"), FakeProvider("b")

    with pytest.raises(RuntimeError, match="close failed"):
        await FallbackProvider([first, second]).aclose()

    assert (first.closed, second.closed) == (1, 1)


async def test_aclose_skips_members_without_aclose() -> None:
    class Bare:
        name = "bare"
        context_window = 1

        async def respond(self, request: ModelRequest) -> ModelResponse:
            return response_of("bare")

        def respond_stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
            raise NotImplementedError

    member = FakeProvider("m")
    await FallbackProvider([Bare(), member]).aclose()

    assert member.closed == 1


# --- build_provider ----------------------------------------------------------------------------

BASE = "https://up.example/v1"


def config(*fallbacks: FallbackModelConfig, wire_api: str = "responses") -> ProviderConfig:
    return ProviderConfig(
        base_url=BASE,
        model="primary-m",
        context_window=1000,
        api_key=Secret("sk-primary"),
        reasoning=ReasoningTiers(),
        wire_api=wire_api,  # type: ignore[arg-type]
        fallbacks=fallbacks,
    )


def fallback_of(model: str, wire_api: str, **overrides: object) -> FallbackModelConfig:
    fields: dict[str, object] = {
        "model": model,
        "wire_api": wire_api,
        "base_url": BASE,
        "api_key": Secret("sk-primary"),
        "context_window": 1000,
    } | overrides
    return FallbackModelConfig(**fields)  # type: ignore[arg-type]


def test_build_provider_without_fallbacks_is_the_bare_adapter() -> None:
    assert isinstance(build_provider(config()), ResponsesProvider)


def test_build_provider_with_fallbacks_wraps_members_in_order() -> None:
    cfg = config(
        fallback_of("grok-4.7", "responses"),
        fallback_of("gem", "gemini", context_window=2000, base_url="https://g.example/v1beta"),
        fallback_of("cc", "chat"),
    )

    provider = build_provider(cfg)

    assert isinstance(provider, FallbackProvider)
    members = provider._providers
    assert [type(m) for m in members] == [
        ResponsesProvider,
        ResponsesProvider,
        GeminiProvider,
        ChatCompletionsProvider,
    ]
    assert [m.name for m in members] == ["primary-m", "grok-4.7", "gem", "cc"]
    assert members[2].context_window == 2000
    assert provider.name == "primary-m"
    assert provider.context_window == 1000


def test_build_provider_gemini_primary() -> None:
    assert isinstance(build_provider(config(wire_api="gemini")), GeminiProvider)


def test_build_provider_members_inherit_retry_settings() -> None:
    cfg = ProviderConfig(
        base_url=BASE,
        model="p",
        context_window=1000,
        api_key=Secret("sk"),
        reasoning=ReasoningTiers(),
        retry_attempts=5,
        retry_backoff=(0.5,),
        fallbacks=(fallback_of("f", "gemini"),),
    )

    provider = build_provider(cfg)

    assert isinstance(provider, FallbackProvider)
    for member in provider._providers:
        assert isinstance(member, ResponsesProvider | GeminiProvider)
        assert member._retry_attempts == 5
        assert member._retry_backoff == (0.5,)


def test_build_provider_rejects_unknown_fallback_wire_api() -> None:
    with pytest.raises(ConfigError, match=r"provider\.fallbacks\[0\]\.wire_api"):
        build_provider(config(fallback_of("f", "grpc")))
