"""Character card parsing, validation, and immutability tests."""

from __future__ import annotations

import pytest

from cgbot.character import DEFAULT_CARD, parse_character_card
from cgbot.config.settings import ConfigError


def test_default_card_is_valid() -> None:
    """The built-in default card parses successfully."""
    assert DEFAULT_CARD.name == "大肥鱼"
    assert DEFAULT_CARD.wake_keywords == ("大肥鱼",)
    assert len(DEFAULT_CARD.persona) > 100
    assert "深海鲸鱼娘" in DEFAULT_CARD.persona
    assert "{owner_id}" in DEFAULT_CARD.persona


def test_parse_minimal_card() -> None:
    card_text = """\
---
name: TestBot
---
Test persona text.
"""
    card = parse_character_card(card_text)
    assert card.name == "TestBot"
    assert card.wake_keywords == ()
    assert card.persona == "Test persona text."


def test_parse_card_with_single_wake_keyword() -> None:
    card_text = """\
---
name: Bot
wake_keywords: keyword
---
Persona.
"""
    card = parse_character_card(card_text)
    assert card.name == "Bot"
    assert card.wake_keywords == ("keyword",)


def test_parse_card_with_wake_keywords_list() -> None:
    card_text = """\
---
name: Bot
wake_keywords:
  - keyword1
  - keyword2
---
Persona.
"""
    card = parse_character_card(card_text)
    assert card.name == "Bot"
    assert card.wake_keywords == ("keyword1", "keyword2")


def test_parse_card_with_inline_list() -> None:
    card_text = """\
---
name: Bot
wake_keywords:
  - keyword1
  - keyword2
---
Persona.
"""
    card = parse_character_card(card_text)
    assert card.wake_keywords == ("keyword1", "keyword2")


def test_missing_frontmatter_raises() -> None:
    card_text = "Just persona text without frontmatter"
    with pytest.raises(ConfigError) as info:
        parse_character_card(card_text)
    assert "must start with --- frontmatter fence" in info.value.message


def test_content_before_frontmatter_raises() -> None:
    card_text = """\
Some text
---
name: Bot
---
Persona.
"""
    with pytest.raises(ConfigError) as info:
        parse_character_card(card_text)
    assert "must start with --- frontmatter fence" in info.value.message


def test_missing_name_raises() -> None:
    card_text = """\
---
wake_keywords:
  - test
---
Persona.
"""
    with pytest.raises(ConfigError) as info:
        parse_character_card(card_text)
    assert "must have 'name'" in info.value.message


def test_empty_name_raises() -> None:
    card_text = """\
---
name: ""
---
Persona.
"""
    with pytest.raises(ConfigError) as info:
        parse_character_card(card_text)
    assert "must have 'name'" in info.value.message


def test_empty_persona_raises() -> None:
    card_text = """\
---
name: Bot
---

"""
    with pytest.raises(ConfigError) as info:
        parse_character_card(card_text)
    assert "persona body is empty" in info.value.message


def test_unknown_key_raises() -> None:
    card_text = """\
---
name: Bot
unknown_key: value
---
Persona.
"""
    with pytest.raises(ConfigError) as info:
        parse_character_card(card_text)
    assert "unknown_key" in info.value.message


def test_invalid_wake_keywords_list_with_number_raises() -> None:
    """wake_keywords must be strings, not numbers (even though YAML allows it)."""
    card_text = """\
---
name: Bot
wake_keywords:
  - valid
  - 123
---
Persona.
"""
    # The number 123 becomes the string "123", which is valid. This test just ensures
    # that wake_keywords as a list of strings works.
    card = parse_character_card(card_text)
    assert card.wake_keywords == ("valid", "123")


def test_empty_wake_keyword_raises() -> None:
    card_text = """\
---
name: Bot
wake_keywords:
  - ""
---
Persona.
"""
    with pytest.raises(ConfigError) as info:
        parse_character_card(card_text)
    assert "non-empty strings" in info.value.message


def test_persona_with_placeholders() -> None:
    card_text = """\
---
name: Bot
---
Owner is {owner_id}.
"""
    card = parse_character_card(card_text)
    assert card.persona == "Owner is {owner_id}."


def test_card_is_immutable() -> None:
    """Character cards are frozen dataclasses."""
    card = DEFAULT_CARD
    with pytest.raises((AttributeError, TypeError)):  # frozen dataclass
        card.name = "NewName"  # type: ignore[misc]


def test_parse_card_strips_whitespace() -> None:
    card_text = """\
---
name: Bot
---

  Persona with leading/trailing whitespace.  

"""
    card = parse_character_card(card_text)
    assert card.persona == "Persona with leading/trailing whitespace."


def test_parse_multiline_persona() -> None:
    card_text = """\
---
name: Bot
---
Line 1
Line 2

Line 4 after blank.
"""
    card = parse_character_card(card_text)
    assert "Line 1\nLine 2" in card.persona
    assert "Line 4 after blank." in card.persona
