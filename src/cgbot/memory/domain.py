"""Visibility domains and viewers for the memory book (R2.5, DESIGN.md §7, §12.1b).

A memory entry carries two domain labels:

- ``domain``: where the entry may be read (``private`` or ``group:<id>``).
- ``source_domain``: the domain of the material it was derived from (provenance).

Reads are decided by a :class:`Viewer` alone. A viewer has one domain and a fixed set of
*subject users*; an entry is visible only when ``domain``, ``source_domain`` and
``subject_user`` all match the viewer (see ``store.py``). Nothing is decided by the model.

Domain set (this slice): ``private`` and ``group:<id>``. A ``public`` domain is not defined
yet (open decision, R2.5-T01); unknown domains are refused on both read and write.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

PRIVATE_DOMAIN = "private"
GROUP_DOMAIN_PREFIX = "group:"
PRIVATE_SESSION_PREFIX = "private:"

_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def check_id(value: object, what: str) -> str:
    """Validate an internal user id or a group id; returns it unchanged."""
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"invalid {what}")
    return value


def group_domain(group_id: str) -> str:
    """``group:<id>`` for a group id."""
    return GROUP_DOMAIN_PREFIX + check_id(group_id, "group id")


def is_valid_domain(domain: object) -> bool:
    if not isinstance(domain, str):
        return False
    if domain == PRIVATE_DOMAIN:
        return True
    return domain.startswith(GROUP_DOMAIN_PREFIX) and bool(
        _ID.fullmatch(domain[len(GROUP_DOMAIN_PREFIX) :])
    )


def check_domain(domain: object) -> str:
    if not is_valid_domain(domain):
        raise ValueError("invalid domain: expected 'private' or 'group:<id>'")
    assert isinstance(domain, str)
    return domain


@dataclass(frozen=True, slots=True)
class Viewer:
    """Who reads or writes: one ``domain`` and the subject users whose entries it may see.

    - ``Viewer.private(user_id)``: the owner's private domain, subjects = {owner}.
    - ``Viewer.group(group_id, participants)``: the group domain, subjects = participants.
      An empty participant set sees nothing (fail closed).
    """

    domain: str
    subjects: frozenset[str]

    def __post_init__(self) -> None:
        check_domain(self.domain)
        if not isinstance(self.subjects, frozenset):
            raise TypeError("subjects must be a frozenset")
        for user_id in self.subjects:
            check_id(user_id, "user id")

    @classmethod
    def private(cls, user_id: str) -> Viewer:
        return cls(PRIVATE_DOMAIN, frozenset({check_id(user_id, "user id")}))

    @classmethod
    def group(cls, group_id: str, participants: Iterable[str]) -> Viewer:
        return cls(group_domain(group_id), frozenset(participants))
