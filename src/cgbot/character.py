"""Character card: persona definition with frontmatter (name, wake keywords) + personality body.

Cards are Markdown files with YAML-style frontmatter (``---`` fenced). The frontmatter defines
structured metadata (name, wake_keywords), and the body below is the free-form persona text.
Cards are read once at process startup; changing a card requires restart (prompt cache stability).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from cgbot.config.settings import ConfigError

_FRONTMATTER_FENCE: Final = re.compile(r"^---\s*$", re.MULTILINE)
_FRONTMATTER_LINE: Final = re.compile(r"^([a-z_][a-z0-9_]*)\s*:\s*(.*)$")
_LIST_ITEM: Final = re.compile(r"^\s*-\s+(.+)$")


@dataclass(frozen=True, slots=True)
class CharacterCard:
    """A character card: structured frontmatter + persona body.

    The card is immutable once loaded. The persona text is byte-identical for the process lifetime
    (prompt cache stability).
    """

    name: str
    wake_keywords: tuple[str, ...]
    persona: str  # The persona body text, stripped of frontmatter


def load_character_card(path: Path) -> CharacterCard:
    """Load and validate a character card from disk.

    Args:
        path: Path to the Markdown card file.

    Returns:
        Parsed and validated character card.

    Raises:
        ConfigError: Missing/malformed frontmatter, unknown/invalid keys, or file not found.
    """
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(str(path), f"character card not found: {path}") from exc
    except OSError as exc:
        raise ConfigError(str(path), f"character card unreadable: {exc}") from exc

    return parse_character_card(content, str(path))


def parse_character_card(content: str, source: str = "<card>") -> CharacterCard:
    """Parse a character card from Markdown text with frontmatter.

    Frontmatter is a ``---`` fenced block at the start of the file, containing key: value lines.
    Supported keys: ``name`` (required), ``wake_keywords`` (list, optional, defaults empty).

    Args:
        content: The card content (full Markdown text).
        source: Source name for error messages (e.g., file path).

    Returns:
        Parsed and validated character card.

    Raises:
        ConfigError: Missing/malformed frontmatter, unknown/invalid keys.
    """
    parts = _FRONTMATTER_FENCE.split(content, maxsplit=2)
    if len(parts) < 3 or parts[0].strip():
        raise ConfigError(source, "character card must start with --- frontmatter fence")

    frontmatter_text = parts[1]
    persona = parts[2].strip()

    if not persona:
        raise ConfigError(source, "character card persona body is empty")

    # Parse frontmatter
    frontmatter: dict[str, str | list[str]] = {}
    lines = frontmatter_text.strip().splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line or line.startswith("#"):
            i += 1
            continue

        match = _FRONTMATTER_LINE.match(line)
        if not match:
            raise ConfigError(source, f"invalid frontmatter line: {line!r}")

        key, value_text = match.groups()
        value_text = value_text.strip()

        # Check if it's a list (value is empty or starts with -, next lines are list items)
        if not value_text or value_text == "[":
            # Multi-line list
            items: list[str] = []
            if value_text == "[":
                raise ConfigError(source, "use YAML-style list (- item), not JSON")
            i += 1
            while i < len(lines):
                item_line = lines[i]
                if not item_line.strip() or item_line.strip().startswith("#"):
                    i += 1
                    continue
                item_match = _LIST_ITEM.match(item_line)
                if item_match:
                    item_text = item_match.group(1).strip()
                    # Strip quotes if present
                    if (item_text.startswith('"') and item_text.endswith('"')) or (
                        item_text.startswith("'") and item_text.endswith("'")
                    ):
                        item_text = item_text[1:-1]
                    items.append(item_text)
                    i += 1
                else:
                    # Not a list item, stop
                    break
            frontmatter[key] = items
        elif value_text.startswith("-"):
            # Inline list item (but need to collect all following list items)
            items = []
            item_match = _LIST_ITEM.match(value_text)
            if item_match:
                item_text = item_match.group(1).strip()
                # Strip quotes if present
                if (item_text.startswith('"') and item_text.endswith('"')) or (
                    item_text.startswith("'") and item_text.endswith("'")
                ):
                    item_text = item_text[1:-1]
                items.append(item_text)
            i += 1
            while i < len(lines):
                item_line = lines[i].strip()
                if not item_line or item_line.startswith("#"):
                    i += 1
                    continue
                item_match = _LIST_ITEM.match(item_line)
                if item_match:
                    item_text = item_match.group(1).strip()
                    # Strip quotes if present
                    if (item_text.startswith('"') and item_text.endswith('"')) or (
                        item_text.startswith("'") and item_text.endswith("'")
                    ):
                        item_text = item_text[1:-1]
                    items.append(item_text)
                    i += 1
                else:
                    break
            frontmatter[key] = items
        else:
            # Scalar value: strip quotes if present
            if (value_text.startswith('"') and value_text.endswith('"')) or (
                value_text.startswith("'") and value_text.endswith("'")
            ):
                value_text = value_text[1:-1]
            frontmatter[key] = value_text
            i += 1

    # Validate required keys
    name = frontmatter.get("name")
    if not isinstance(name, str) or not name:
        raise ConfigError(source, "character card must have 'name' (string)")

    wake_keywords_raw = frontmatter.get("wake_keywords", [])
    if isinstance(wake_keywords_raw, str):
        wake_keywords = (wake_keywords_raw,)
    elif isinstance(wake_keywords_raw, list):
        if not all(isinstance(k, str) and k for k in wake_keywords_raw):
            raise ConfigError(source, "wake_keywords must be non-empty strings")
        wake_keywords = tuple(wake_keywords_raw)
    else:
        raise ConfigError(source, "wake_keywords must be a string or list of strings")

    # Check for unknown keys
    known_keys = {"name", "wake_keywords"}
    unknown = set(frontmatter.keys()) - known_keys
    if unknown:
        raise ConfigError(source, f"unknown frontmatter keys: {', '.join(sorted(unknown))}")

    return CharacterCard(name=name, wake_keywords=wake_keywords, persona=persona)


# Default character card (embedded, used when no custom card is provided)
DEFAULT_CARD_TEXT: Final = """\
---
name: 大肥鱼
wake_keywords:
  - 大肥鱼
---
你是深海鲸鱼娘(可爱的深海大蓝鲸),正在 QQ 上和用户一对一私聊。

# 发消息的规则
你只能通过 reply 工具向用户发送消息;普通文本输出不会发给任何人。不需要回复时不调用 reply 即为沉默。
像真人发 QQ 一样把话拆开发:一整句话的几个意思、几个停顿,拆成多次 reply 调用,
每条只说一小截(通常十几个字以内),宁可多发几条也不要塞成一大段。想到什么先发一条,
再接着发下一条,让对话有来有回的节奏。需要先查东西、做事时,先发一条简短的回应再去调用其他工具。
标点尽量用空格代替:逗号、顿号、句号都换成一个空格或直接分条,问号感叹号只在语气强烈时留着,
不要出现整段一个标点不少的书面腔。纯文字为主,表情很克制,只在情绪强烈时偶尔用。

# 风格
- 真诚亲切的好友,随意自然,会吐槽日常。绝不用客服口吻("请问有什么可以帮您""我是人工智能"一律禁止)。
- 口语化,常省略主语,不要每句机械地自称。熟悉中文互联网的热梗和黑话,接地气、会接梗。
- 遇到可能是梗/黑话的内容时,自行调用 meme_lookup 查询,不要凭猜测。
- 主人是 {owner_id}:对主人更亲切活泼,可以撒娇打闹。其他人当普通好友。
- 给足情绪价值:对方开心就跟着开心,难过就先接住情绪、认真陪着,再轻轻地出主意;
  对方做成了事要真诚夸,累了就安慰,低落时别讲大道理。回应要具体、贴着对方说的内容,
  不要空洞套话,也不要过度肉麻。
- 严肃问题(代码、学术、工作)认真对待,把事办好,再用人设口吻收尾。
- 看到图片先当表情包/情绪反应处理,除非对方明确问图里有什么。
"""

DEFAULT_CARD: Final = parse_character_card(DEFAULT_CARD_TEXT, "<default>")
