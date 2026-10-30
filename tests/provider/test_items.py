"""Offline tests for Responses input-item helpers (R1.5-U02 pass-through rules)."""

from __future__ import annotations

from cgbot.provider import (
    FunctionCall,
    Reasoning,
    TextOutput,
    function_call_output,
    item_from_output,
    user_message,
)


def test_user_message_shape() -> None:
    assert user_message("你好") == {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "你好"}],
    }


def test_function_call_output_shape() -> None:
    assert function_call_output("call_1", "42") == {
        "type": "function_call_output",
        "call_id": "call_1",
        "output": "42",
    }


def test_reasoning_keeps_encrypted_content_and_drops_stored_ids() -> None:
    raw = {
        "type": "reasoning",
        "id": "rs_123",
        "status": "completed",
        "summary": [{"type": "summary_text", "text": "思考"}],
        "encrypted_content": "ENC",
    }
    item = Reasoning(summary="思考", raw=raw)

    assert item_from_output(item) == {
        "type": "reasoning",
        "summary": [{"type": "summary_text", "text": "思考"}],
        "encrypted_content": "ENC",
    }


def test_message_drops_id_and_status_keeps_content() -> None:
    raw = {
        "type": "message",
        "id": "msg_1",
        "status": "completed",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "好", "annotations": []}],
    }

    assert item_from_output(TextOutput(text="好", raw=raw)) == {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "好", "annotations": []}],
    }


def test_function_call_from_raw_mapping_and_typed_item_agree() -> None:
    raw = {
        "type": "function_call",
        "id": "fc_1",
        "call_id": "call_1",
        "name": "reply",
        "arguments": '{"text": "x"}',
        "status": "completed",
    }
    typed = FunctionCall(
        call_id="call_1",
        name="reply",
        arguments='{"text": "x"}',
        parsed_arguments={"text": "x"},
        parse_error=None,
        raw=raw,
    )
    expected = {
        "type": "function_call",
        "call_id": "call_1",
        "name": "reply",
        "arguments": '{"text": "x"}',
    }

    assert item_from_output(raw) == expected
    assert item_from_output(typed) == expected


def test_result_is_a_deep_copy_of_the_raw_item() -> None:
    raw = {"type": "reasoning", "summary": [{"type": "summary_text", "text": "a"}]}
    out = item_from_output(raw)

    out["summary"][0]["text"] = "changed"  # type: ignore[index]

    assert raw["summary"] == [{"type": "summary_text", "text": "a"}]
