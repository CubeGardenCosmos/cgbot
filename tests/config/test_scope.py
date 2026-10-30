"""R1.1-U03: ScopeConfig.allows truth table (offline)."""

from __future__ import annotations

import pytest

from cgbot.config import ScopeConfig

SCOPE = ScopeConfig(
    owner="9999",
    friends=frozenset({"1001", "1002"}),
    groups=frozenset({"2001"}),
)


@pytest.mark.parametrize(
    ("user_id", "expected"),
    [
        ("1001", True),  # configured friend
        ("1002", True),
        ("3003", False),  # stranger
        ("9999", False),  # owner is an identity marker, not auto-allowed
    ],
)
def test_private_allowlist(user_id: str, expected: bool) -> None:
    assert SCOPE.allows("private", user_id, None) is expected


@pytest.mark.parametrize(
    ("user_id", "group_id", "expected"),
    [
        ("1001", "2001", True),  # friend in configured group
        ("3003", "2001", True),  # any member of a configured group is in scope
        ("1001", "2002", False),  # unconfigured group
        ("3003", "2002", False),
    ],
)
def test_group_allowlist(user_id: str, group_id: str, expected: bool) -> None:
    assert SCOPE.allows("group", user_id, group_id) is expected


def test_group_without_group_id_is_denied() -> None:
    assert SCOPE.allows("group", "1001", None) is False
    assert SCOPE.allows("group", "3003", None) is False


def test_empty_scope_denies_everything() -> None:
    empty = ScopeConfig(owner=None, friends=frozenset(), groups=frozenset())
    assert empty.allows("private", "1001", None) is False
    assert empty.allows("group", "1001", "2001") is False
    assert empty.allows("group", "1001", None) is False


def test_unknown_scope_raises() -> None:
    with pytest.raises(ValueError):
        SCOPE.allows("channel", "1001", None)  # type: ignore[arg-type]
