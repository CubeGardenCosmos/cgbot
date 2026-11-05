"""Render-time message timestamps: user/system messages and sent ``reply`` outputs (offline)."""

from __future__ import annotations

import copy
import time
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from cgbot.agent.history import COMPACTION_PREFIX, render_items, to_input_items
from cgbot.provider import user_message
from cgbot.session import Item, Kind
from cgbot.timefmt import format_stamp, time_instruction
from cgbot.vision import message_with_images

KL = ZoneInfo("Asia/Kuala_Lumpur")
SID = "private:u1"
# 2026-11-05 01:41 UTC = 09:41 in Kuala Lumpur (UTC+8), a Thursday.
T1 = datetime(2026, 11, 5, 1, 41, tzinfo=UTC).timestamp()
STAMP1 = "[2026-11-05 09:41 周四]"
T2 = T1 + 90 * 60  # 11:11 the same day
STAMP2 = "[2026-11-05 11:11 周四]"


def _item(seq: int, kind: Kind, payload: dict[str, Any], ts: float) -> Item:
    return Item(seq=seq, session_id=SID, kind=kind, payload=payload, ts=ts)


def _call(seq: int, call_id: str, name: str, ts: float) -> Item:
    item = {"type": "function_call", "call_id": call_id, "name": name, "arguments": "{}"}
    payload = {"call_id": call_id, "name": name, "arguments": "{}", "item": item}
    return _item(seq, "tool_call", payload, ts)


def _result(
    seq: int, call_id: str, name: str, output: str, ts: float, *, error: bool = False
) -> Item:
    payload = {"call_id": call_id, "name": name, "output": output, "is_error": error}
    return _item(seq, "tool_result", payload, ts)


def _window() -> list[Item]:
    assistant = {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "想想"}],
    }
    return [
        _item(1, "user", {"item": user_message("在吗")}, T1),
        _item(2, "assistant", {"item": assistant, "internal": True}, T1 + 1),
        _call(3, "c1", "reply", T1 + 2),
        _result(4, "c1", "reply", "sent via qq:42", T1 + 3),
        _item(5, "user", {"item": user_message("吃了吗")}, T2),
    ]


def test_format_is_local_minute_precision_with_chinese_weekday() -> None:
    assert format_stamp(T1, KL) == STAMP1
    assert format_stamp(T1 + 59, KL) == STAMP1  # no seconds
    assert format_stamp(T1, ZoneInfo("UTC")) == "[2026-11-05 01:41 周四]"
    sunday = datetime(2026, 11, 8, 12, 0, tzinfo=UTC).timestamp()
    assert format_stamp(sunday, KL) == "[2026-11-08 20:00 周日]"


def test_time_instruction_names_the_zone() -> None:
    note = time_instruction(KL)
    assert "方括号时间" in note and "Asia/Kuala_Lumpur" in note


def test_each_user_message_and_reply_output_carries_its_own_time() -> None:
    items = to_input_items(_window(), tz=KL)

    assert [i["type"] for i in items] == [
        "message",
        "message",
        "function_call",
        "function_call_output",
        "message",
    ]
    assert items[0]["content"] == [
        {"type": "input_text", "text": f"{STAMP1} "},
        {"type": "input_text", "text": "在吗"},
    ]
    assert items[3] == {
        "type": "function_call_output",
        "call_id": "c1",
        "output": f"{STAMP1} sent via qq:42",
    }
    assert items[4]["content"][0]["text"] == f"{STAMP2} "
    assert items[4]["content"][1]["text"] == "吃了吗"


def test_internal_assistant_text_and_calls_are_untouched() -> None:
    items = to_input_items(_window(), tz=KL)
    assert items[1]["content"] == [{"type": "output_text", "text": "想想"}]
    assert items[2]["name"] == "reply" and "arguments" in items[2]


def test_call_output_pairing_is_intact() -> None:
    items = to_input_items(_window(), tz=KL)
    calls = [i["call_id"] for i in items if i["type"] == "function_call"]
    outputs = [i["call_id"] for i in items if i["type"] == "function_call_output"]
    assert calls == outputs == ["c1"]
    assert items.index(next(i for i in items if i["type"] == "function_call_output")) == (
        items.index(next(i for i in items if i["type"] == "function_call")) + 1
    )


def test_failed_reply_and_other_tools_are_not_stamped() -> None:
    window = [
        _call(1, "c1", "reply", T1),
        _result(2, "c1", "reply", "send failed: boom", T1, error=True),
        _call(3, "c2", "meme_lookup", T1),
        _result(4, "c2", "meme_lookup", "梗: 解释", T1),
    ]
    outputs = [i["output"] for i in to_input_items(window, tz=KL) if i["type"].endswith("_output")]
    assert outputs == ["send failed: boom", "梗: 解释"]


def test_system_event_message_is_stamped_and_observations_are_not_input() -> None:
    window = [
        _item(1, "system_event", {"item": user_message("[系统通知] 任务完成")}, T2),
        _item(2, "system_event", {"event": "model_failed", "error": "X"}, T2),
    ]
    items = to_input_items(window, tz=KL)
    assert len(items) == 1
    assert items[0]["content"][0]["text"] == f"{STAMP2} "
    assert items[0]["content"][1]["text"] == "[系统通知] 任务完成"


def test_images_and_other_parts_are_kept_after_the_stamp() -> None:
    window = [_item(1, "user", {"item": message_with_images("看[图片]", ["a.jpg", "b.jpg"])}, T1)]
    items = render_items(window, {"a.jpg": "data:image/jpeg;base64,AA"}, tz=KL)
    assert items[0]["content"] == [
        {"type": "input_text", "text": f"{STAMP1} "},
        {"type": "input_text", "text": "看[图片]"},
        {"type": "input_image", "image_url": "data:image/jpeg;base64,AA"},
    ]


def test_string_content_is_prefixed() -> None:
    window = [_item(1, "user", {"item": {"type": "message", "role": "user", "content": "hi"}}, T1)]
    assert to_input_items(window, tz=KL)[0]["content"] == f"{STAMP1} hi"


def test_compaction_summary_is_not_stamped() -> None:
    summary = _item(9, "compaction", {"summary": "S", "retained": []}, T1)
    assert to_input_items([summary], tz=KL) == [user_message(COMPACTION_PREFIX + "S")]


def test_retained_rows_keep_their_original_time() -> None:
    retained = [
        {"seq": 4, "kind": "user", "payload": {"item": user_message("旧")}, "ts": T1},
        {"seq": 5, "kind": "tool_call", "payload": _call(5, "c9", "reply", T1).payload, "ts": T1},
        {
            "seq": 6,
            "kind": "tool_result",
            "payload": _result(6, "c9", "reply", "sent", T1).payload,
            "ts": T1 + 5,
        },
    ]
    window = [_item(10, "compaction", {"summary": "S", "retained": retained}, T2)]
    items = to_input_items(window, tz=KL)
    assert items[1]["content"][0]["text"] == f"{STAMP1} "
    assert items[3]["output"] == f"{STAMP1} sent"


def test_rendering_is_stable_and_does_not_mutate_the_stored_rows() -> None:
    window = _window()
    before = copy.deepcopy([i.payload for i in window])
    first = to_input_items(window, tz=KL)
    second = to_input_items(window, tz=KL)
    assert first == second
    assert [i.payload for i in window] == before  # stamps are never stored


def test_zone_is_the_configured_one_not_the_process_tz(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = to_input_items(_window(), tz=KL)
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    try:
        assert to_input_items(_window(), tz=KL) == expected
        other = to_input_items(_window(), tz=ZoneInfo("America/Los_Angeles"))
        assert other[0]["content"][0]["text"] == "[2026-11-04 17:41 周三] "
    finally:
        monkeypatch.undo()
        time.tzset()
