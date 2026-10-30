"""Offline unit tests for the pure OneBot11 codec (R1.2-U01, R1.2-U02)."""

from __future__ import annotations

import time

import pytest

from cgbot.model import InboundMessage, InputStatus, OutboundMessage, Segment
from cgbot.napcat import build_send, parse_event

SELF_ID = 10001


def private_event(message: object, **overrides: object) -> dict[str, object]:
    raw: dict[str, object] = {
        "time": 1761800000,
        "self_id": SELF_ID,
        "post_type": "message",
        "message_type": "private",
        "sub_type": "friend",
        "message_id": 555,
        "user_id": 20002,
        "message": message,
        "raw_message": "[redacted]",
    }
    raw.update(overrides)
    return raw


def input_status_event(**overrides: object) -> dict[str, object]:
    raw: dict[str, object] = {
        "time": 1761800100,
        "self_id": SELF_ID,
        "post_type": "notice",
        "notice_type": "notify",
        "sub_type": "input_status",
        "user_id": 20002,
        "group_id": 0,
        "event_type": 1,
        "status_text": "对方正在输入...",
    }
    raw.update(overrides)
    return raw


# --- R1.2-U01: array message -> internal model -> send action -----------------------------

MIXED_MESSAGE = [
    {"type": "reply", "data": {"id": "500"}},
    {"type": "text", "data": {"text": "看这个"}},
    {"type": "at", "data": {"qq": "10001"}},
    {"type": "image", "data": {"file": "a.png", "url": "https://example.invalid/a.png"}},
    {"type": "record", "data": {"file": "v.silk"}},
    {"type": "video", "data": {"file": "c.mp4"}},
    {"type": "file", "data": {"file": "d.zip", "name": "d.zip"}},
    {"type": "face", "data": {"id": 178}},
    {"type": "forward", "data": {"id": 9876}},
    {"type": "json", "data": {"data": "{}"}},
]


def test_array_message_segments_are_ordered_and_typed() -> None:
    msg = parse_event(private_event(MIXED_MESSAGE))
    assert isinstance(msg, InboundMessage)
    assert [s.type for s in msg.segments] == [
        "reply",
        "text",
        "at",
        "image",
        "record",
        "video",
        "file",
        "face",
        "forward",
        "other",
    ]
    assert msg.segments[1] == Segment("text", {"text": "看这个"})
    assert msg.segments[-1] == Segment("other", {"onebot_type": "json", "data": "{}"})


def test_private_message_identity_fields_not_mixed() -> None:
    msg = parse_event(private_event(MIXED_MESSAGE))
    assert isinstance(msg, InboundMessage)
    assert msg.message_id == "555"  # own id, not the quoted one
    assert msg.reply_to == "500"  # quoted id, not own id
    assert msg.user_id == "20002"  # sender, not self_id
    assert msg.scope == "private"
    assert msg.group_id is None
    assert msg.time == 1761800000.0


def test_numeric_ids_normalised_to_str() -> None:
    msg = parse_event(
        private_event([{"type": "at", "data": {"qq": 10001}}, {"type": "reply", "data": {"id": 7}}])
    )
    assert isinstance(msg, InboundMessage)
    assert msg.segments[0] == Segment("at", {"qq": "10001"})
    assert msg.reply_to == "7"


def test_reply_to_none_without_reply_segment() -> None:
    msg = parse_event(private_event([{"type": "text", "data": {"text": "x"}}]))
    assert isinstance(msg, InboundMessage)
    assert msg.reply_to is None


def test_group_message_parsed_with_group_scope() -> None:
    raw = private_event(
        [{"type": "text", "data": {"text": "hi"}}],
        message_type="group",
        sub_type="normal",
        group_id=30003,
    )
    msg = parse_event(raw)
    assert isinstance(msg, InboundMessage)
    assert msg.scope == "group"
    assert msg.group_id == "30003"
    assert msg.user_id == "20002"


def test_outbound_from_inbound_keeps_sender_and_quote_ids_apart() -> None:
    msg = parse_event(private_event(MIXED_MESSAGE))
    assert isinstance(msg, InboundMessage)
    reply = OutboundMessage(
        scope=msg.scope,
        target_id=msg.user_id,
        segments=(Segment("text", {"text": "好的"}),),
        reply_to=msg.message_id,
    )
    action, params = build_send(reply)
    assert action == "send_private_msg"
    assert params == {
        "user_id": 20002,
        "message": [
            {"type": "reply", "data": {"id": "555"}},
            {"type": "text", "data": {"text": "好的"}},
        ],
    }


# --- R1.2-U02: input_status samples ----------------------------------------------------------


def test_input_status_fields_preserved() -> None:
    status = parse_event(input_status_event(time=1761800100.5))
    assert status == InputStatus(
        user_id="20002", event_type=1, status_text="对方正在输入...", time=1761800100.5
    )


def test_empty_status_text_is_not_a_stop_event() -> None:
    raw = input_status_event(event_type=2, status_text="")
    status = parse_event(raw)
    assert isinstance(status, InputStatus)
    assert status.status_text == ""
    assert status.event_type == 2
    assert status.user_id == "20002"


def test_input_status_group_id_nonzero_ignored() -> None:
    assert parse_event(input_status_event(group_id=30003)) is None


@pytest.mark.parametrize("event_type", ["1", True, None])
def test_input_status_malformed_event_type_ignored(event_type: object) -> None:
    assert parse_event(input_status_event(event_type=event_type)) is None


# --- parse_event: ignored events ---------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        {"time": 1, "self_id": SELF_ID, "post_type": "meta_event", "meta_event_type": "heartbeat"},
        {
            "time": 1,
            "self_id": SELF_ID,
            "post_type": "meta_event",
            "meta_event_type": "lifecycle",
            "sub_type": "connect",
        },
        {"time": 1, "self_id": SELF_ID, "post_type": "message_sent", "message_type": "private"},
        input_status_event(sub_type="poke"),
        {"time": 1, "self_id": SELF_ID, "post_type": "notice", "notice_type": "group_increase"},
        {"time": 1, "self_id": SELF_ID, "post_type": "request", "request_type": "friend"},
    ],
    ids=["heartbeat", "lifecycle", "message_sent", "notify-poke", "group_increase", "request"],
)
def test_non_message_events_ignored(raw: dict[str, object]) -> None:
    assert parse_event(raw) is None


def test_self_message_ignored() -> None:
    assert parse_event(private_event(MIXED_MESSAGE, user_id=SELF_ID)) is None


def test_string_cq_message_not_decoded() -> None:
    assert parse_event(private_event("[CQ:image,file=a.png]")) is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"message_type": "guild"},
        {"message_id": None},
        {"user_id": None},
        {"message_type": "group", "group_id": None},
        {"self_id": None},
    ],
    ids=["unknown-type", "no-message-id", "no-user", "group-no-group-id", "no-self-id"],
)
def test_malformed_message_ignored(overrides: dict[str, object]) -> None:
    assert parse_event(private_event([], **overrides)) is None


def test_missing_time_falls_back_to_receive_time() -> None:
    before = time.time()
    msg = parse_event(private_event([], time=None))
    assert isinstance(msg, InboundMessage)
    assert before <= msg.time <= time.time()


# --- build_send -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            OutboundMessage("private", "20002", (Segment("text", {"text": "hi"}),)),
            (
                "send_private_msg",
                {"user_id": 20002, "message": [{"type": "text", "data": {"text": "hi"}}]},
            ),
        ),
        (
            OutboundMessage(
                "group",
                "30003",
                (Segment("at", {"qq": "20002"}), Segment("text", {"text": "x"})),
                reply_to="88",
            ),
            (
                "send_group_msg",
                {
                    "group_id": 30003,
                    "message": [
                        {"type": "reply", "data": {"id": "88"}},
                        {"type": "at", "data": {"qq": "20002"}},
                        {"type": "text", "data": {"text": "x"}},
                    ],
                },
            ),
        ),
        (
            OutboundMessage(
                "private",
                "20002",
                (
                    Segment("image", {"file": "base64://AAAA"}),
                    Segment("record", {"file": "/tmp/v.wav"}),
                    Segment("video", {"file": "c.mp4"}),
                    Segment("file", {"file": "d.zip", "name": "d.zip"}),
                    Segment("face", {"id": 1}),
                ),
            ),
            (
                "send_private_msg",
                {
                    "user_id": 20002,
                    "message": [
                        {"type": "image", "data": {"file": "base64://AAAA"}},
                        {"type": "record", "data": {"file": "/tmp/v.wav"}},
                        {"type": "video", "data": {"file": "c.mp4"}},
                        {"type": "file", "data": {"file": "d.zip", "name": "d.zip"}},
                        {"type": "face", "data": {"id": 1}},
                    ],
                },
            ),
        ),
        (
            OutboundMessage("private", "20002", (Segment("reply", {"id": "9"}),)),
            (
                "send_private_msg",
                {"user_id": 20002, "message": [{"type": "reply", "data": {"id": "9"}}]},
            ),
        ),
    ],
    ids=["private-text", "group-reply-at", "media-passthrough", "inline-reply"],
)
def test_build_send_table(
    message: OutboundMessage, expected: tuple[str, dict[str, object]]
) -> None:
    assert build_send(message) == expected


@pytest.mark.parametrize(
    ("message", "reason"),
    [
        (OutboundMessage("private", "20002", (Segment("forward", {"id": "1"}),)), "forward"),
        (
            OutboundMessage("private", "20002", (Segment("other", {"onebot_type": "json"}),)),
            "other",
        ),
        (OutboundMessage("private", "abc", (Segment("text", {"text": "x"}),)), "non-numeric"),
        (
            OutboundMessage("private", "20002", (Segment("reply", {"id": "1"}),), reply_to="2"),
            "double-reply",
        ),
        (OutboundMessage("guild", "20002", (Segment("text", {"text": "x"}),)), "scope"),  # pyright: ignore[reportArgumentType]
    ],
    ids=["forward", "other", "non-numeric-target", "double-reply", "unknown-scope"],
)
def test_build_send_rejects_inexpressible(message: OutboundMessage, reason: str) -> None:
    with pytest.raises(ValueError):
        build_send(message)
