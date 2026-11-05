from __future__ import annotations

from typing import Any
from zoneinfo import ZoneInfo

from cgbot.agent.history import COMPACTION_PREFIX, expand_window, to_input_items
from cgbot.provider import user_message
from cgbot.session import Item

TZ = ZoneInfo("Asia/Kuala_Lumpur")


def _row(seq: int, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"seq": seq, "kind": kind, "payload": payload, "ts": 1.0}


def _compaction(seq: int, retained: list[dict[str, Any]]) -> Item:
    payload: dict[str, Any] = {"summary": "S", "retained": retained}
    return Item(seq=seq, session_id="private:u1", kind="compaction", payload=payload, ts=1.0)


def _call_row(seq: int, call_id: str) -> dict[str, Any]:
    item = {"type": "function_call", "call_id": call_id, "name": "f", "arguments": "{}"}
    payload = {"call_id": call_id, "name": "f", "arguments": "{}", "item": item}
    return _row(seq, "tool_call", payload)


def _result_row(seq: int, call_id: str) -> dict[str, Any]:
    return _row(
        seq, "tool_result", {"call_id": call_id, "name": "f", "output": "r", "is_error": False}
    )


def test_retained_rows_follow_the_summary_with_their_original_seq() -> None:
    window = [
        _compaction(10, [_row(3, "user", {"item": user_message("保留的问题")})]),
        Item(
            seq=11,
            session_id="private:u1",
            kind="user",
            payload={"item": user_message("新问题")},
            ts=2.0,
        ),
    ]
    expanded = expand_window(window)
    assert [i.seq for i in expanded] == [10, 3, 11]
    assert expanded[1].kind == "user"
    assert expanded[1].session_id == "private:u1"


def test_summary_and_retained_pair_render_as_a_valid_tool_exchange() -> None:
    window = [_compaction(9, [_call_row(4, "c1"), _result_row(5, "c1")])]
    items = to_input_items(window, tz=TZ)
    assert items[0] == user_message(COMPACTION_PREFIX + "S")
    assert [i["type"] for i in items[1:]] == ["function_call", "function_call_output"]
    assert items[2]["call_id"] == "c1"


def test_output_whose_call_was_summarised_away_is_dropped() -> None:
    window = [_compaction(9, [_result_row(5, "gone")])]
    assert to_input_items(window, tz=TZ) == [user_message(COMPACTION_PREFIX + "S")]


def test_without_retained_rows_only_the_summary_is_rendered() -> None:
    window = [_compaction(9, [])]
    assert to_input_items(window, tz=TZ) == [user_message(COMPACTION_PREFIX + "S")]
