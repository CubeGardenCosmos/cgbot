from __future__ import annotations

from cgbot.group.mute import is_mute_trigger, is_muted, mute_until
from cgbot.group.text import contains_keyword, keyword_text, mentions_user, plain_text
from cgbot.model import Segment


def test_plain_text_renders_text_at_and_placeholders() -> None:
    segments = (
        Segment("at", {"qq": "999"}),
        Segment("text", {"text": " hi "}),
        Segment("image", {"file": "x.png"}),
        Segment("reply", {"id": "m1"}),
        Segment("face", {"id": "1"}),
        Segment("other", {"raw": 1}),
    )
    assert plain_text(segments) == "@999 hi [图片][表情]"


def test_keyword_text_excludes_at_targets_and_placeholders() -> None:
    segments = (
        Segment("at", {"qq": "999"}),
        Segment("text", {"text": "大肥鱼"}),
        Segment("image", {"file": "x.png"}),
    )
    assert keyword_text(segments) == "大肥鱼"


def test_contains_keyword_is_case_insensitive_and_ignores_empty_keywords() -> None:
    assert contains_keyword("Hello BOT", ["bot"]) is True
    assert contains_keyword("anything", [""]) is False
    assert contains_keyword("anything", []) is False
    assert contains_keyword("hi", ["", "hi"]) is True


def test_mentions_user_matches_only_at_segments_for_that_id() -> None:
    assert mentions_user((Segment("at", {"qq": "999"}),), "999") is True
    assert mentions_user((Segment("at", {"qq": "123"}),), "999") is False
    assert mentions_user((Segment("text", {"text": "999"}),), "999") is False


def test_mute_is_active_strictly_before_expiry() -> None:
    until = mute_until(1000.0, 72 * 3600.0)
    assert until == 1000.0 + 72 * 3600.0
    assert is_muted(until, until - 0.001) is True
    assert is_muted(until, until) is False
    assert is_muted(None, 0.0) is False


def test_mute_trigger_requires_addressing_and_keyword() -> None:
    assert is_mute_trigger(True, "闭嘴 please", ["闭嘴"]) is True
    assert is_mute_trigger(False, "闭嘴", ["闭嘴"]) is False
    assert is_mute_trigger(True, "hello", ["闭嘴"]) is False
    assert is_mute_trigger(True, "闭嘴", []) is False
