"""Visibility matrix and write-rule tests for the memory book (R2.5-U01, U02, U03)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from cgbot.memory import CrossDomainWriteError, MemoryBook, Viewer, group_domain


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
async def book(tmp_path) -> AsyncIterator[MemoryBook]:
    b = MemoryBook(tmp_path / "memory.sqlite", clock=Clock())
    yield b
    await b.close()


PRIV_U1 = Viewer.private("u1")
PRIV_U2 = Viewer.private("u2")
GROUP_G1 = Viewer.group("g1", ["u1", "u2"])
GROUP_G2 = Viewer.group("g2", ["u1"])


async def _seed(book: MemoryBook) -> dict[str, int]:
    """Private memory of u1, private memory of u2, and group-profile entries in g1 and g2."""
    ids = {}
    ids["priv_u1"] = (
        await book.add(
            subject_user="u1",
            domain="private",
            source_domain="private",
            text="u1 private secret 旅行计划",
        )
    ).id
    ids["priv_u2"] = (
        await book.add(
            subject_user="u2",
            domain="private",
            source_domain="private",
            text="u2 private secret 旅行计划",
        )
    ).id
    ids["g1_u1"] = (
        await book.add_group_profile("g1", "u1", "u1 likes tea in g1 群画像", tags=["tea"])
    ).id
    ids["g1_u2"] = (await book.add_group_profile("g1", "u2", "u2 likes coffee in g1")).id
    ids["g2_u1"] = (await book.add_group_profile("g2", "u1", "u1 likes tea in g2")).id
    return ids


async def test_private_viewer_sees_only_own_private_entries(book: MemoryBook) -> None:
    ids = await _seed(book)
    got = await book.search("secret", viewer=PRIV_U1, limit=10)
    assert [e.id for e in got] == [ids["priv_u1"]]
    got_u2 = await book.search("secret", viewer=PRIV_U2, limit=10)
    assert [e.id for e in got_u2] == [ids["priv_u2"]]


async def test_private_viewer_never_sees_group_domain_entries(book: MemoryBook) -> None:
    await _seed(book)
    # U01: a user's own group profile is not readable from the private chat.
    assert await book.search("tea", viewer=PRIV_U1, limit=10) == []
    assert await book.list_for_prompt(viewer=PRIV_U1, budget_chars=10_000) != []
    listed = await book.list_for_prompt(viewer=PRIV_U1, budget_chars=10_000)
    assert all(e.domain == "private" for e in listed)


async def test_group_viewer_sees_only_its_group_and_participants(book: MemoryBook) -> None:
    ids = await _seed(book)
    got = await book.search("likes", viewer=GROUP_G1, limit=10)
    assert {e.id for e in got} == {ids["g1_u1"], ids["g1_u2"]}
    # g2 entries are invisible to g1, even for a shared participant.
    assert ids["g2_u1"] not in {e.id for e in got}


async def test_group_viewer_never_sees_private_entries(book: MemoryBook) -> None:
    await _seed(book)
    # U01/U02: private-domain material never enters a group turn, even for a participant.
    assert await book.search("secret", viewer=GROUP_G1, limit=10) == []
    listed = await book.list_for_prompt(viewer=GROUP_G1, budget_chars=10_000)
    assert {e.domain for e in listed} == {group_domain("g1")}


async def test_group_viewer_without_participants_sees_nothing(book: MemoryBook) -> None:
    await _seed(book)
    empty = Viewer.group("g1", [])
    assert await book.search("likes", viewer=empty, limit=10) == []
    assert await book.list_for_prompt(viewer=empty, budget_chars=10_000) == []


async def test_user_filter_narrows_but_never_widens(book: MemoryBook) -> None:
    ids = await _seed(book)
    only_u1 = await book.search("likes", viewer=GROUP_G1, user_filter=["u1"], limit=10)
    assert [e.id for e in only_u1] == [ids["g1_u1"]]
    # A filter for a user outside the viewer's subjects yields nothing.
    outsider = await book.search("likes", viewer=GROUP_G1, user_filter=["u9"], limit=10)
    assert outsider == []
    listed = await book.list_for_prompt(viewer=GROUP_G1, budget_chars=10_000, user_filter=["u9"])
    assert listed == []


async def test_add_group_profile_visible_only_to_its_group_viewer(book: MemoryBook) -> None:
    entry = await book.add_group_profile("g1", "u1", "profile fact")
    assert entry.domain == entry.source_domain == "group:g1"
    assert (await book.get(entry.id, viewer=GROUP_G1)) == entry
    assert await book.get(entry.id, viewer=GROUP_G2) is None
    assert await book.get(entry.id, viewer=PRIV_U1) is None


async def test_cross_domain_writes_are_refused(book: MemoryBook) -> None:
    cases = [
        ("private", "group:g1"),  # private material into a group
        ("group:g1", "private"),  # group material into private
        ("group:g2", "group:g1"),  # cross-group promotion
    ]
    for domain, source in cases:
        with pytest.raises(CrossDomainWriteError):
            await book.add(subject_user="u1", domain=domain, source_domain=source, text="x")


async def test_invalid_domain_is_refused(book: MemoryBook) -> None:
    for bad in ["public", "group:", "group:a b", "Private", ""]:
        with pytest.raises(ValueError):
            await book.add(subject_user="u1", domain=bad, source_domain=bad, text="x")


async def test_get_update_delete_are_scoped_to_visibility(book: MemoryBook) -> None:
    ids = await _seed(book)
    target = ids["priv_u1"]
    # Invisible to other viewers: indistinguishable from absence.
    assert await book.get(target, viewer=PRIV_U2) is None
    assert await book.update(target, viewer=PRIV_U2, text="hijack") is None
    assert await book.delete(target, viewer=PRIV_U2) is False
    assert await book.delete(target, viewer=GROUP_G1) is False
    # Still intact for the owner.
    owner = await book.get(target, viewer=PRIV_U1)
    assert owner is not None
    assert owner.text.startswith("u1 private secret")


async def test_domains_are_immutable_through_update(book: MemoryBook) -> None:
    entry = await book.add_group_profile("g1", "u1", "fact")
    updated = await book.update(entry.id, viewer=GROUP_G1, text="fact v2")
    assert updated is not None
    assert updated.domain == updated.source_domain == "group:g1"
    assert updated.subject_user == "u1"
