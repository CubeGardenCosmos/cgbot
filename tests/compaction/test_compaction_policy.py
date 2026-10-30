from __future__ import annotations

import json
import math
from typing import Any

import pytest

from cgbot.compaction.policy import (
    choose_cut,
    estimate_tokens,
    over_threshold,
    transcript,
)
from cgbot.provider import user_message
from cgbot.session import Item, Kind


def _item(seq: int, kind: Kind, payload: dict[str, Any]) -> Item:
    return Item(seq=seq, session_id="private:u1", kind=kind, payload=payload, ts=0.0)


def _user(seq: int, text: str = "hi") -> Item:
    return _item(seq, "user", {"item": user_message(text)})


def _assistant(seq: int, text: str = "ok") -> Item:
    item = {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }
    return _item(seq, "assistant", {"item": item, "internal": True})


def _call(seq: int, call_id: str) -> Item:
    return _item(seq, "tool_call", {"call_id": call_id, "name": "f", "arguments": "{}", "item": {}})


def _result(seq: int, call_id: str) -> Item:
    return _item(
        seq, "tool_result", {"call_id": call_id, "name": "f", "output": "r", "is_error": False}
    )


def test_threshold_is_inclusive_at_fifty_percent() -> None:
    assert over_threshold(499, 1000) is False
    assert over_threshold(500, 1000) is True
    assert over_threshold(501, 1000) is True
    assert over_threshold(1, 2) is True
    assert over_threshold(1, 3) is False


def test_estimate_is_utf8_bytes_over_three_rounded_up() -> None:
    items = [user_message("你好")]
    raw = json.dumps(items, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert estimate_tokens(items) == math.ceil(len(raw) / 3)
    assert estimate_tokens([]) == 1  # "[]" is two bytes
    assert estimate_tokens([user_message("a" * 300)]) > estimate_tokens([user_message("a")])


def test_cut_keeps_last_k_exchanges() -> None:
    # exchanges start at indices 0, 2, 4, 6
    window = [
        _user(1),
        _assistant(2),
        _user(3),
        _assistant(4),
        _user(5),
        _assistant(6),
        _user(7),
        _assistant(8),
    ]
    assert choose_cut(window, keep_exchanges=3) == 2
    assert choose_cut(window, keep_exchanges=1) == 6
    assert choose_cut(window, keep_exchanges=4) == 0


def test_cut_with_fewer_exchanges_than_k_summarises_nothing() -> None:
    assert choose_cut([_user(1), _assistant(2)], keep_exchanges=3) == 0
    assert choose_cut([], keep_exchanges=3) == 0


def test_cut_never_splits_a_tool_call_from_its_result() -> None:
    # user(0) call(1) injected user(2) result(3) assistant(4) user(5) assistant(6)
    window = [
        _user(1),
        _call(2, "c1"),
        _user(3, "injected"),
        _result(4, "c1"),
        _assistant(5),
        _user(6),
        _assistant(7),
    ]
    # K=2 would put the cut at 2, inside call..result; it moves past the result.
    assert choose_cut(window, keep_exchanges=2) == 4


@pytest.mark.parametrize("keep", [1, 2, 3, 4])
def test_cut_is_never_inside_a_pair_and_tail_never_starts_with_output(keep: int) -> None:
    window = [
        _user(1),
        _call(2, "c1"),
        _item(3, "system_event", {"item": user_message("event")}),
        _result(4, "c1"),
        _assistant(5),
        _user(6),
        _call(7, "c2"),
        _result(8, "c2"),
        _assistant(9),
    ]
    cut = choose_cut(window, keep_exchanges=keep)
    assert 0 <= cut <= len(window)
    if cut < len(window):
        assert window[cut].kind != "tool_result"
    for a, b in ((1, 3), (6, 7)):
        assert not (a < cut <= b)


def test_unanswered_call_stays_in_the_summarised_head() -> None:
    # user(0) user(1) call(2, unanswered) assistant(3); K=1 wants the cut at 1.
    window = [_user(1), _user(2), _call(3, "c1"), _assistant(4)]
    cut = choose_cut(window, keep_exchanges=1)
    assert cut == 3
    assert all(item.kind != "tool_call" for item in window[cut:])


def test_transcript_labels_roles_and_skips_reasoning() -> None:
    summary_row = _item(0, "compaction", {"summary": "旧摘要"})
    reasoning = _item(
        5, "assistant", {"item": {"type": "reasoning", "summary": []}, "internal": True}
    )
    text = transcript(
        [
            summary_row,
            _user(1, "你好"),
            _assistant(2, "嗨"),
            _call(3, "c1"),
            _result(4, "c1"),
            reasoning,
            _item(6, "system_event", {"message_id": "x"}),
        ]
    )
    assert text == "\n".join(
        [
            "[此前摘要] 旧摘要",
            "用户: 你好",
            "助手: 嗨",
            "[工具调用 f] {}",
            "[工具结果 f] r",
        ]
    )
