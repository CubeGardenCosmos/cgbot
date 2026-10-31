"""Group SKILL cards, session summaries, profile sink and the group-side SQLite store (R3.4/R3.5).

Card format v1 (JSON text, one row per group in table ``group_skill_card``)::

    {
      "schema": "cgbot.group_skill", "version": 1, "group_id": "<gid>", "revision": 3,
      "updated_at": <unix seconds>, "atmosphere": "<text>",
      "topics": ["..."], "memes": ["..."], "rules": ["..."],
      "members": [{"user_id": "<qq>", "role": "<text>|null", "habits": ["..."],
                   "last_seen": <unix seconds>}],
      "last_session": {"session_id": "group:<gid>", "ended_at": <s>, "end_reason": "<reason>"}
    }

Merge policy: new summary items come first, duplicates (casefold) are dropped, each list is
capped by ``SkillLimits``. Members are kept by most recent ``last_seen`` (not insertion order).
Only users who participated in the processed session may appear in a summary.

Store: ``StoragePaths.for_module("group_skill")``. Tables ``schema_version``, ``group_skill_card``
and ``group_session`` (lifecycle ledger for active/ended group sessions, see ``SessionRecord``).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from cgbot.group.tools import GROUP_REPLY_TOOL_NAME
from cgbot.provider import ModelProvider, ModelRequest, TextOutput, user_message
from cgbot.session import Item

CARD_SCHEMA = "cgbot.group_skill"
CARD_VERSION = 1
SCHEMA_COMPONENT = "group_skill"
SCHEMA_VERSION = 1

SessionStatus = Literal["active", "ended"]


class SkillCardError(ValueError):
    """Stored or supplied card text is not a valid card of a supported version."""


class SummaryError(ValueError):
    """Model output could not be turned into a session summary."""


@dataclass(frozen=True, slots=True)
class SkillLimits:
    """Per-list caps. Values are supplied by configuration; there is no built-in default."""

    topics: int
    memes: int
    rules: int
    members: int
    habits_per_member: int


@dataclass(frozen=True, slots=True)
class MemberNote:
    user_id: str
    role: str | None
    habits: tuple[str, ...]
    last_seen: float


@dataclass(frozen=True, slots=True)
class LastSession:
    session_id: str
    ended_at: float
    end_reason: str


@dataclass(frozen=True, slots=True)
class SkillCard:
    group_id: str
    revision: int
    updated_at: float
    atmosphere: str
    topics: tuple[str, ...]
    memes: tuple[str, ...]
    rules: tuple[str, ...]
    members: tuple[MemberNote, ...]
    last_session: LastSession | None

    def to_json(self) -> str:
        data: dict[str, Any] = {
            "schema": CARD_SCHEMA,
            "version": CARD_VERSION,
            "group_id": self.group_id,
            "revision": self.revision,
            "updated_at": self.updated_at,
            "atmosphere": self.atmosphere,
            "topics": list(self.topics),
            "memes": list(self.memes),
            "rules": list(self.rules),
            "members": [
                {
                    "user_id": m.user_id,
                    "role": m.role,
                    "habits": list(m.habits),
                    "last_seen": m.last_seen,
                }
                for m in self.members
            ],
            "last_session": None
            if self.last_session is None
            else {
                "session_id": self.last_session.session_id,
                "ended_at": self.last_session.ended_at,
                "end_reason": self.last_session.end_reason,
            },
        }
        return json.dumps(data, ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> SkillCard:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            raise SkillCardError("card is not valid JSON") from None
        if not isinstance(data, dict):
            raise SkillCardError("card must be a JSON object")
        if data.get("schema") != CARD_SCHEMA:
            raise SkillCardError("card schema mismatch")
        if data.get("version") != CARD_VERSION:
            raise SkillCardError(f"unsupported card version: {data.get('version')!r}")
        last_raw = data.get("last_session")
        last: LastSession | None = None
        if last_raw is not None:
            if not isinstance(last_raw, dict):
                raise SkillCardError("last_session must be an object or null")
            last = LastSession(
                session_id=_req_str(last_raw, "session_id"),
                ended_at=_req_float(last_raw, "ended_at"),
                end_reason=_req_str(last_raw, "end_reason"),
            )
        members_raw = data.get("members")
        if not isinstance(members_raw, list):
            raise SkillCardError("members must be a list")
        members = tuple(_member_from(m) for m in members_raw)
        return cls(
            group_id=_req_str(data, "group_id"),
            revision=int(_req_float(data, "revision")),
            updated_at=_req_float(data, "updated_at"),
            atmosphere=_req_str(data, "atmosphere", allow_empty=True),
            topics=_req_str_list(data, "topics"),
            memes=_req_str_list(data, "memes"),
            rules=_req_str_list(data, "rules"),
            members=members,
            last_session=last,
        )


def _member_from(raw: object) -> MemberNote:
    if not isinstance(raw, dict):
        raise SkillCardError("member entry must be an object")
    role = raw.get("role")
    if role is not None and not isinstance(role, str):
        raise SkillCardError("member role must be a string or null")
    return MemberNote(
        user_id=_req_str(raw, "user_id"),
        role=role,
        habits=_req_str_list(raw, "habits"),
        last_seen=_req_float(raw, "last_seen"),
    )


def _req_str(data: Mapping[str, Any], key: str, *, allow_empty: bool = False) -> str:
    value = data.get(key)
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise SkillCardError(f"{key} must be a non-empty string")
    return value


def _req_float(data: Mapping[str, Any], key: str) -> float:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise SkillCardError(f"{key} must be a number")
    return float(value)


def _req_str_list(data: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = data.get(key)
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise SkillCardError(f"{key} must be a list of strings")
    return tuple(value)


def render_card_text(card: SkillCard) -> str:
    """Card as instruction text for the next group session (this group's own card only)."""
    lines = [f"群气氛: {card.atmosphere or '(未记录)'}"]
    if card.topics:
        lines.append("常聊话题: " + "; ".join(card.topics))
    if card.rules:
        lines.append("群内约定: " + "; ".join(card.rules))
    if card.members:
        lines.append("成员印象:")
        for m in card.members:
            role = f"({m.role})" if m.role else ""
            habits = "; ".join(m.habits) if m.habits else "(无)"
            lines.append(f"- {m.user_id}{role}: {habits}")
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class MemberObservation:
    user_id: str
    role: str | None
    habits: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SessionSummary:
    atmosphere: str
    topics: tuple[str, ...]
    memes: tuple[str, ...]
    rules: tuple[str, ...]
    members: tuple[MemberObservation, ...]


SUMMARY_INSTRUCTIONS = (
    "你是群聊会话总结器。输入是一段已结束的群聊记录(成员消息与机器人回复)。"
    "只依据记录内容总结,不要编造,不要推断记录之外的个人信息。"
    "只输出一个 JSON 对象,不要其他文字,键如下:\n"
    '{"atmosphere": "本段群气氛,一句话", "topics": ["常见话题"], "memes": ["群内梗或黑话"], '
    '"rules": ["群内约定"], "members": [{"user_id": "记录中出现的 QQ 号", '
    '"role": "角色或null", "habits": ["发言习惯或偏好"]}]}\n'
    "members 只能包含记录中实际发言的成员 user_id。"
)


def render_transcript(items: Sequence[Item]) -> str:
    """Processed group session as plain text: member messages and bot replies only."""
    lines: list[str] = []
    for item in items:
        payload = item.payload
        if item.kind in ("user", "system_event") and "item" in payload:
            text = _item_text(payload["item"])
            if text:
                lines.append(text)
        elif item.kind == "tool_call" and payload.get("name") == GROUP_REPLY_TOOL_NAME:
            arguments = payload.get("item", {}).get("arguments", "")
            try:
                reply = json.loads(arguments).get("text", "")
            except (json.JSONDecodeError, AttributeError):
                reply = ""
            if isinstance(reply, str) and reply:
                lines.append(f"[机器人回复] {reply}")
    return "\n".join(lines)


def _item_text(item: object) -> str:
    if not isinstance(item, Mapping):
        return ""
    content = item.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(part.get("text", "")) for part in content if isinstance(part, Mapping))
    return ""


async def summarize_transcript(
    provider: ModelProvider, transcript: str, *, reasoning_effort: str | None
) -> SessionSummary:
    """One model call over a processed session transcript; raises ``SummaryError`` on bad output."""
    response = await provider.respond(
        ModelRequest(
            instructions=SUMMARY_INSTRUCTIONS,
            input=[user_message(transcript)],
            reasoning_effort=reasoning_effort,
        )
    )
    text = "".join(item.text for item in response.items if isinstance(item, TextOutput))
    return parse_summary(text)


def parse_summary(text: str) -> SessionSummary:
    body = text.strip()
    if body.startswith("```"):
        body = body.strip("`").removeprefix("json").strip()
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        raise SummaryError("summary output is not JSON") from None
    if not isinstance(data, dict):
        raise SummaryError("summary output must be a JSON object")
    atmosphere = data.get("atmosphere", "")
    if not isinstance(atmosphere, str):
        raise SummaryError("atmosphere must be a string")
    members_raw = data.get("members", [])
    if not isinstance(members_raw, list):
        raise SummaryError("members must be a list")
    members: list[MemberObservation] = []
    for raw in members_raw:
        if not isinstance(raw, dict) or not isinstance(raw.get("user_id"), str):
            raise SummaryError("member entry needs a string user_id")
        role = raw.get("role")
        if role is not None and not isinstance(role, str):
            raise SummaryError("member role must be a string or null")
        members.append(
            MemberObservation(
                user_id=raw["user_id"].strip(),
                role=role,
                habits=_summary_list(raw, "habits"),
            )
        )
    return SessionSummary(
        atmosphere=atmosphere.strip(),
        topics=_summary_list(data, "topics"),
        memes=_summary_list(data, "memes"),
        rules=_summary_list(data, "rules"),
        members=tuple(members),
    )


def _summary_list(data: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise SummaryError(f"{key} must be a list of strings")
    return tuple(v.strip() for v in value if v.strip())


def _merge_list(new: Sequence[str], old: Sequence[str], cap: int) -> tuple[str, ...]:
    out: list[str] = []
    seen: set[str] = set()
    for value in (*new, *old):
        cleaned = value.strip()
        key = cleaned.casefold()
        if not cleaned or key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
        if len(out) >= cap:
            break
    return tuple(out)


def merge_card(
    prev: SkillCard | None,
    summary: SessionSummary,
    *,
    group_id: str,
    session_id: str,
    now: float,
    end_reason: str,
    limits: SkillLimits,
) -> SkillCard:
    members: dict[str, MemberNote] = {m.user_id: m for m in prev.members} if prev else {}
    for obs in summary.members:
        old = members.get(obs.user_id)
        members[obs.user_id] = MemberNote(
            user_id=obs.user_id,
            role=obs.role or (old.role if old else None),
            habits=_merge_list(obs.habits, old.habits if old else (), limits.habits_per_member),
            last_seen=now,
        )
    ordered = sorted(members.values(), key=lambda m: m.last_seen, reverse=True)
    return SkillCard(
        group_id=group_id,
        revision=(prev.revision + 1) if prev else 1,
        updated_at=now,
        atmosphere=summary.atmosphere or (prev.atmosphere if prev else ""),
        topics=_merge_list(summary.topics, prev.topics if prev else (), limits.topics),
        memes=_merge_list(summary.memes, prev.memes if prev else (), limits.memes),
        rules=_merge_list(summary.rules, prev.rules if prev else (), limits.rules),
        members=tuple(ordered[: limits.members]),
        last_session=LastSession(session_id=session_id, ended_at=now, end_reason=end_reason),
    )


def group_domain(group_id: str) -> str:
    """Memory-book visibility domain for a group."""
    return f"group:{group_id}"


@dataclass(frozen=True, slots=True)
class ProfileEntry:
    group_id: str
    user_id: str
    domain: str
    role: str | None
    habits: tuple[str, ...]
    source_session_id: str
    ended_at: float


def profile_entries(
    *, group_id: str, session_id: str, ended_at: float, summary: SessionSummary
) -> tuple[ProfileEntry, ...]:
    return tuple(
        ProfileEntry(
            group_id=group_id,
            user_id=obs.user_id,
            domain=group_domain(group_id),
            role=obs.role,
            habits=obs.habits,
            source_session_id=session_id,
            ended_at=ended_at,
        )
        for obs in summary.members
    )


class ProfileSink(Protocol):
    """Receives per-member profile entries (memory book lives elsewhere; best-effort)."""

    async def write(self, entries: Sequence[ProfileEntry]) -> None: ...


class InMemoryProfileSink:
    """Test/dev sink that keeps every entry in order."""

    def __init__(self) -> None:
        self.entries: list[ProfileEntry] = []

    async def write(self, entries: Sequence[ProfileEntry]) -> None:
        self.entries.extend(entries)


@dataclass(frozen=True, slots=True)
class SessionRecord:
    row_id: int
    group_id: str
    session_id: str
    first_seq: int
    started_at: float
    last_activity: float
    turn_count: int
    participants: tuple[str, ...]
    status: SessionStatus
    end_reason: str | None
    summary_status: str | None
    ended_at: float | None


class GroupSkillStore:
    """SQLite store for group cards and the group session ledger (file: ``group_skill``)."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            path, timeout=5.0, isolation_level=None, check_same_thread=False
        )
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version "
                "(component TEXT PRIMARY KEY, version INTEGER NOT NULL)"
            )
            self._conn.execute(
                "INSERT INTO schema_version (component, version) VALUES (?, ?) "
                "ON CONFLICT (component) DO NOTHING",
                (SCHEMA_COMPONENT, SCHEMA_VERSION),
            )
            row = self._conn.execute(
                "SELECT version FROM schema_version WHERE component = ?", (SCHEMA_COMPONENT,)
            ).fetchone()
            if int(row[0]) > SCHEMA_VERSION:
                raise RuntimeError(f"group_skill schema version {row[0]} is newer than supported")
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS group_skill_card ("
                "group_id TEXT PRIMARY KEY, card TEXT NOT NULL, revision INTEGER NOT NULL, "
                "updated_at REAL NOT NULL)"
            )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS group_session ("
                "row_id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "group_id TEXT NOT NULL, session_id TEXT NOT NULL, first_seq INTEGER NOT NULL, "
                "started_at REAL NOT NULL, last_activity REAL NOT NULL, "
                "turn_count INTEGER NOT NULL, participants TEXT NOT NULL, "
                "status TEXT NOT NULL CHECK (status IN ('active', 'ended')), "
                "end_reason TEXT, summary_status TEXT, ended_at REAL)"
            )
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS group_session_one_active "
                "ON group_session (group_id) WHERE status = 'active'"
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    async def get_card(self, group_id: str) -> SkillCard | None:
        return await asyncio.to_thread(self._get_card, group_id)

    def _get_card(self, group_id: str) -> SkillCard | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT card FROM group_skill_card WHERE group_id = ?", (group_id,)
            ).fetchone()
        return None if row is None else SkillCard.from_json(str(row[0]))

    async def put_card(self, card: SkillCard) -> None:
        await asyncio.to_thread(self._put_card, card)

    def _put_card(self, card: SkillCard) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO group_skill_card (group_id, card, revision, updated_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT (group_id) DO UPDATE SET "
                "card = excluded.card, revision = excluded.revision, "
                "updated_at = excluded.updated_at",
                (card.group_id, card.to_json(), card.revision, card.updated_at),
            )

    async def open_session(
        self, *, group_id: str, session_id: str, first_seq: int, now: float
    ) -> int:
        return await asyncio.to_thread(self._open_session, group_id, session_id, first_seq, now)

    def _open_session(self, group_id: str, session_id: str, first_seq: int, now: float) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO group_session (group_id, session_id, first_seq, started_at, "
                "last_activity, turn_count, participants, status) "
                "VALUES (?, ?, ?, ?, ?, 0, '[]', 'active')",
                (group_id, session_id, first_seq, now, now),
            )
            row_id = cur.lastrowid
        if row_id is None:
            raise RuntimeError("group_session insert returned no row id")
        return row_id

    async def update_session(
        self,
        row_id: int,
        *,
        participants: Sequence[str],
        turn_count: int,
        last_activity: float,
    ) -> None:
        await asyncio.to_thread(
            self._update_session, row_id, tuple(participants), turn_count, last_activity
        )

    def _update_session(
        self, row_id: int, participants: tuple[str, ...], turn_count: int, last_activity: float
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE group_session SET participants = ?, turn_count = ?, last_activity = ? "
                "WHERE row_id = ? AND status = 'active'",
                (
                    json.dumps(list(participants), ensure_ascii=False),
                    turn_count,
                    last_activity,
                    row_id,
                ),
            )

    async def finish_session(
        self, row_id: int, *, end_reason: str, summary_status: str, now: float
    ) -> None:
        await asyncio.to_thread(self._finish_session, row_id, end_reason, summary_status, now)

    def _finish_session(
        self, row_id: int, end_reason: str, summary_status: str, now: float
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE group_session SET status = 'ended', end_reason = ?, summary_status = ?, "
                "ended_at = ? WHERE row_id = ? AND status = 'active'",
                (end_reason, summary_status, now, row_id),
            )

    async def active_sessions(self) -> list[SessionRecord]:
        return await asyncio.to_thread(self._active_sessions)

    def _active_sessions(self) -> list[SessionRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT row_id, group_id, session_id, first_seq, started_at, last_activity, "
                "turn_count, participants, status, end_reason, summary_status, ended_at "
                "FROM group_session WHERE status = 'active' ORDER BY row_id"
            ).fetchall()
        return [
            SessionRecord(
                row_id=int(r[0]),
                group_id=str(r[1]),
                session_id=str(r[2]),
                first_seq=int(r[3]),
                started_at=float(r[4]),
                last_activity=float(r[5]),
                turn_count=int(r[6]),
                participants=tuple(json.loads(r[7])),
                status="active",
                end_reason=None,
                summary_status=None,
                ended_at=None,
            )
            for r in rows
        ]
