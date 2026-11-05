from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

from compaction_fakes import SID, TZ, FakeProvider, Rows

from cgbot.agent.history import COMPACTION_PREFIX, to_input_items
from cgbot.compaction import Compactor, estimate_tokens
from cgbot.compaction.policy import DEFAULT_SUMMARY_INSTRUCTIONS
from cgbot.config.settings import ReasoningTiers
from cgbot.provider import ProviderError, Usage
from cgbot.session import AsyncSessionStore

PAD = "填充" * 60


def _compactor(
    provider: FakeProvider,
    store: AsyncSessionStore,
    *,
    busy: bool = False,
    tiers: ReasoningTiers | None = None,
    reserved_tokens: int = 0,
) -> Compactor:
    return Compactor(
        provider,
        store,
        None,
        tiers or ReasoningTiers(),
        is_busy=lambda _sid: busy,
        tz=TZ,
        reserved_tokens=reserved_tokens,
    )


async def _four_exchanges(rows: Rows) -> dict[str, int]:
    seqs: dict[str, int] = {}
    seqs["u1"] = await rows.user("第一轮问")
    seqs["a1"] = await rows.assistant("第一轮答")
    seqs["u2"] = await rows.user("第二轮问")
    seqs["a2"] = await rows.assistant("第二轮答")
    seqs["u3"] = await rows.user("第三轮问")
    await rows.call("c1")
    await rows.result("c1", "工具输出")
    seqs["a3"] = await rows.assistant("第三轮答")
    seqs["u4"] = await rows.user("第四轮问")
    seqs["a4"] = await rows.assistant("第四轮答")
    return seqs


def _texts(items: list[dict[str, Any]]) -> str:
    out: list[str] = []
    for item in items:
        if item.get("type") == "message":
            for part in item["content"]:
                out.append(str(part.get("text", "")))
        elif item.get("type") == "function_call_output":
            out.append(str(item["output"]))
    return "\n".join(out)


async def test_below_threshold_makes_no_request(
    store: AsyncSessionStore, provider_cls: type[FakeProvider], rows_cls: type[Rows]
) -> None:
    await rows_cls(store).exchange("你好", "你好呀")
    provider = provider_cls(context_window=10_000_000)
    out = await _compactor(provider, store).maybe_compact(SID)
    assert out.status == "below_threshold"
    assert out.tokens is not None and out.tokens > 0
    assert provider.requests == []
    assert len(await store.load(SID)) == 2


async def test_threshold_boundary_is_exactly_half_the_window(
    store: AsyncSessionStore, provider_cls: type[FakeProvider], rows_cls: type[Rows]
) -> None:
    for sid in (SID, "private:u2"):
        rows = Rows(store, sid)
        for i in range(4):
            await rows.exchange(f"{PAD}问{i}", f"{PAD}答{i}")
    tokens = estimate_tokens(to_input_items(await store.load_active(SID), tz=TZ))

    at_half = provider_cls(context_window=2 * tokens)
    out = await _compactor(at_half, store).maybe_compact(SID)
    assert out.status == "compacted"
    assert out.tokens == tokens

    just_below = provider_cls(context_window=2 * tokens + 1)
    out = await _compactor(just_below, store).maybe_compact("private:u2")
    assert out.status == "below_threshold"
    assert just_below.requests == []


async def test_reserved_tokens_count_when_no_usage_is_recorded(
    store: AsyncSessionStore, provider_cls: type[FakeProvider], rows_cls: type[Rows]
) -> None:
    await rows_cls(store).exchange("你好", "你好呀")
    provider = provider_cls(context_window=10_000)
    assert (await _compactor(provider, store).maybe_compact(SID)).status == "below_threshold"
    out = await _compactor(provider, store, reserved_tokens=10_000).maybe_compact(SID)
    assert out.status == "nothing_to_compact"  # over threshold, but one exchange only
    assert out.tokens is not None and out.tokens >= 10_000


async def test_recorded_usage_replaces_the_estimate_for_older_rows(
    store: AsyncSessionStore, provider_cls: type[FakeProvider], rows_cls: type[Rows]
) -> None:
    rows = rows_cls(store)
    seqs = await _four_exchanges(rows)
    provider = provider_cls(context_window=1000)
    compactor = _compactor(provider, store)

    compactor.record_usage(SID, Usage(input_tokens=499), through_seq=seqs["a4"])
    out = await compactor.maybe_compact(SID)
    assert (out.status, out.tokens) == ("below_threshold", 499)

    compactor.record_usage(SID, Usage(input_tokens=500), through_seq=seqs["a4"])
    out = await compactor.maybe_compact(SID)
    assert (out.status, out.tokens) == ("compacted", 500)


async def test_rows_after_the_usage_mark_are_estimated_on_top(
    store: AsyncSessionStore, provider_cls: type[FakeProvider], rows_cls: type[Rows]
) -> None:
    rows = rows_cls(store)
    seqs = await _four_exchanges(rows)
    await rows.user(PAD)
    window = await store.load_active(SID)
    newer = [item for item in window if item.seq > seqs["a4"]]
    expected = 100 + estimate_tokens(to_input_items(newer, tz=TZ))

    compactor = _compactor(provider_cls(context_window=10_000_000), store)
    compactor.record_usage(SID, Usage(input_tokens=100), through_seq=seqs["a4"])
    out = await compactor.maybe_compact(SID)
    assert out.status == "below_threshold"
    assert out.tokens == expected


async def test_compact_summarises_the_head_and_keeps_the_tail_verbatim(
    store: AsyncSessionStore, provider_cls: type[FakeProvider], rows_cls: type[Rows]
) -> None:
    rows = rows_cls(store)
    seqs = await _four_exchanges(rows)
    provider = provider_cls(summary="摘要正文")
    tiers = ReasoningTiers(summary="medium")
    out = await _compactor(provider, store, tiers=tiers).compact(SID)

    assert out.status == "compacted"
    assert out.reason == "manual"
    compaction_seq = out.seq
    assert compaction_seq is not None and compaction_seq > seqs["a4"]

    (request,) = provider.requests
    assert request.instructions == DEFAULT_SUMMARY_INSTRUCTIONS
    assert request.tools == []
    assert request.reasoning_effort == "medium"
    prompt = _texts(request.input)
    assert "用户: 第一轮问" in prompt and "助手: 第一轮答" in prompt
    assert "第二轮问" not in prompt  # the K=3 tail is not summarised
    assert "[工具结果 lookup] 工具输出" not in prompt

    window = await store.load_active(SID)
    assert window[0].kind == "compaction" and window[0].seq == compaction_seq
    items = to_input_items(window, tz=TZ)
    assert items[0]["role"] == "user"
    assert items[0]["content"][0]["text"].startswith(COMPACTION_PREFIX + "摘要正文")
    assert "第二轮问" in _texts(items) and "第四轮答" in _texts(items)
    call_items = [i for i in items if i.get("type") == "function_call"]
    output_items = [i for i in items if i.get("type") == "function_call_output"]
    assert [i["call_id"] for i in call_items] == ["c1"]
    assert [i["call_id"] for i in output_items] == ["c1"]
    assert "第一轮问" not in _texts(items)


async def test_second_compaction_feeds_the_previous_summary_back(
    store: AsyncSessionStore, provider_cls: type[FakeProvider], rows_cls: type[Rows]
) -> None:
    rows = rows_cls(store)
    await _four_exchanges(rows)
    provider = provider_cls(summary="摘要正文")
    compactor = _compactor(provider, store)
    assert (await compactor.compact(SID)).status == "compacted"
    for i in range(5, 8):
        await rows.exchange(f"第{i}轮问", f"第{i}轮答")

    out = await compactor.compact(SID)
    assert out.status == "compacted"
    prompt = _texts(provider.requests[-1].input)
    assert "[此前摘要] 摘要正文" in prompt
    assert "第二轮问" in prompt


async def test_provider_error_writes_nothing_and_reports_only_its_name(
    store: AsyncSessionStore, provider_cls: type[FakeProvider], rows_cls: type[Rows]
) -> None:
    rows = rows_cls(store)
    await _four_exchanges(rows)
    before = await store.load(SID)
    provider = provider_cls(error=ProviderError("upstream secret detail"))
    out = await _compactor(provider, store).compact(SID)
    assert out.status == "failed"
    assert out.error == "ProviderError"
    assert "secret" not in repr(out)
    assert await store.load(SID) == before


async def test_empty_or_incomplete_summary_is_a_failure(
    store: AsyncSessionStore, provider_cls: type[FakeProvider], rows_cls: type[Rows]
) -> None:
    rows = rows_cls(store)
    await _four_exchanges(rows)
    before = await store.load(SID)

    empty = await _compactor(provider_cls(summary=""), store).compact(SID)
    assert (empty.status, empty.error) == ("failed", "CompactionError")

    incomplete = await _compactor(provider_cls(status="incomplete"), store).compact(SID)
    assert (incomplete.status, incomplete.error) == ("failed", "CompactionError")
    assert await store.load(SID) == before


async def test_failure_keeps_the_usage_record(
    store: AsyncSessionStore, provider_cls: type[FakeProvider], rows_cls: type[Rows]
) -> None:
    rows = rows_cls(store)
    seqs = await _four_exchanges(rows)
    provider = provider_cls(context_window=1000, error=ProviderError("down"))
    compactor = _compactor(provider, store)
    compactor.record_usage(SID, Usage(input_tokens=600), through_seq=seqs["a4"])
    assert (await compactor.maybe_compact(SID)).status == "failed"
    provider.error = None
    out = await compactor.maybe_compact(SID)
    assert (out.status, out.tokens) == ("compacted", 600)


async def test_successful_compaction_clears_the_usage_record(
    store: AsyncSessionStore, provider_cls: type[FakeProvider], rows_cls: type[Rows]
) -> None:
    rows = rows_cls(store)
    seqs = await _four_exchanges(rows)
    compactor = _compactor(provider_cls(context_window=10_000_000), store)
    compactor.record_usage(SID, Usage(input_tokens=999), through_seq=seqs["a4"])
    assert (await compactor.compact(SID)).status == "compacted"
    out = await compactor.maybe_compact(SID)
    assert out.status == "below_threshold"
    assert out.tokens is not None and out.tokens < 999


async def test_nothing_to_compact_with_a_single_exchange(
    store: AsyncSessionStore, provider_cls: type[FakeProvider], rows_cls: type[Rows]
) -> None:
    await rows_cls(store).exchange("你好", "你好呀")
    provider = provider_cls()
    out = await _compactor(provider, store).compact(SID)
    assert out.status == "nothing_to_compact"
    assert provider.requests == []


async def test_busy_session_is_not_compacted(
    store: AsyncSessionStore, provider_cls: type[FakeProvider], rows_cls: type[Rows]
) -> None:
    await _four_exchanges(rows_cls(store))
    provider = provider_cls()
    out = await _compactor(provider, store, busy=True).maybe_compact(SID)
    assert out.status == "busy"
    assert provider.requests == []


async def test_turn_starting_during_summary_blocks_the_commit(
    store: AsyncSessionStore, provider_cls: type[FakeProvider], rows_cls: type[Rows]
) -> None:
    await _four_exchanges(rows_cls(store))
    before = await store.load(SID)
    answers: Iterator[bool] = iter([False, True])
    compactor = Compactor(
        provider_cls(),
        store,
        None,
        ReasoningTiers(),
        is_busy=lambda _sid: next(answers),
        tz=TZ,
    )
    out = await compactor.compact(SID)
    assert out.status == "busy"
    assert await store.load(SID) == before


async def test_message_arriving_during_summary_supersedes_the_compaction(
    store: AsyncSessionStore, provider_cls: type[FakeProvider], rows_cls: type[Rows]
) -> None:
    rows = rows_cls(store)
    await _four_exchanges(rows)
    before = await store.load(SID)
    late: list[int] = []

    async def arrive(_request: Any) -> None:
        late.append(await rows.user("总结期间的新消息"))

    provider = provider_cls(on_request=arrive)
    out = await _compactor(provider, store).compact(SID)
    assert out.status == "superseded"
    assert len(provider.requests) == 1
    after = await store.load(SID)
    assert [i.seq for i in after] == [i.seq for i in before] + late
    assert all(i.kind != "compaction" for i in after)


async def test_concurrent_compaction_of_the_same_session_is_refused(
    store: AsyncSessionStore, provider_cls: type[FakeProvider], rows_cls: type[Rows]
) -> None:
    await _four_exchanges(rows_cls(store))
    gate = asyncio.Event()
    entered = asyncio.Event()

    async def enter(_request: Any) -> None:
        entered.set()

    provider = provider_cls(gate=gate, on_request=enter)
    compactor = _compactor(provider, store)
    first = asyncio.create_task(compactor.compact(SID))
    await entered.wait()
    second = await compactor.compact(SID)
    assert second.status == "in_progress"
    gate.set()
    assert (await first).status == "compacted"
