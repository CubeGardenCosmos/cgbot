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


ALL_FRIENDS = ScopeConfig(
    owner=None,
    friends=frozenset({"1001"}),
    groups=frozenset({"2001"}),
    all_friends=True,
)


@pytest.mark.parametrize(
    ("user_id", "is_friend", "expected"),
    [
        ("1001", False, True),  # explicit friends entry is in scope regardless of sub_type
        ("3003", True, True),  # real friend (sub_type "friend") not in the list
        ("3003", False, False),  # temporary session from a group member: not a friend
    ],
)
def test_all_friends_admits_real_friends_only(
    user_id: str, is_friend: bool, expected: bool
) -> None:
    assert ALL_FRIENDS.allows("private", user_id, None, is_friend=is_friend) is expected


def test_is_friend_defaults_to_not_friend() -> None:
    assert ALL_FRIENDS.allows("private", "3003", None) is False


def test_all_friends_defaults_off() -> None:
    assert SCOPE.all_friends is False


def test_flag_off_ignores_is_friend() -> None:
    assert SCOPE.allows("private", "3003", None, is_friend=True) is False
    assert SCOPE.allows("private", "1001", None, is_friend=False) is True


def test_all_friends_does_not_change_group_scope() -> None:
    assert ALL_FRIENDS.allows("group", "3003", "2001", is_friend=True) is True
    assert ALL_FRIENDS.allows("group", "3003", "2002", is_friend=True) is False
    assert ALL_FRIENDS.allows("group", "3003", None, is_friend=True) is False
