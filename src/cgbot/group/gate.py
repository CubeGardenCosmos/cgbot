"""Group gate (M3): decides per inbound group message whether it wakes, follows up, rolls a
proactive opener, triggers a mute, or is ignored (and buffered). No LLM, no network.

Caller contract:
- Call ``note_bot_spoke(gid, ts, message_id)`` after every bot group send. It opens the
  follow-up window and records the bot message id, which is how a reply-to-bot is recognized
  (NapCat's reply segment carries no sender, only the quoted message id).
- ``mute_triggered`` means the caller must end any active group session without a summary.
- ``decide`` performs short blocking sqlite transactions on the event loop thread.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, tzinfo
from typing import Literal

from cgbot.config.settings import GroupConfig, ScopeConfig
from cgbot.group.buffer import DEFAULT_BUFFER_CAPACITY, BufferedMessage, buffered_from
from cgbot.group.mute import is_mute_trigger, is_muted, mute_until
from cgbot.group.proactive import Rng, evaluate_roll, followup_open
from cgbot.group.store import GroupState, GroupStateStore
from cgbot.group.text import contains_keyword, keyword_text, mentions_user
from cgbot.model import InboundMessage

HOUR = 3600.0

GateAction = Literal["ignore", "wake", "followup", "proactive", "mute_triggered"]
GateReason = Literal[
    "out_of_scope",
    "bot_self",
    "sleep",
    "muted",
    "mute_keyword",
    "wake_keyword",
    "member_reply",
    "roll_hit",
    "roll_miss",
    "cooldown",
    "hour_rolled",
    "outside_window",
]


@dataclass(frozen=True, slots=True)
class GatePolicy:
    """Hour bounds are local clock hours; sleep is ``[sleep_start, sleep_end)``."""

    sleep_start_hour: int = 0
    sleep_end_hour: int = 8
    proactive_start_hour: int = 8
    proactive_end_hour: int = 24
    proactive_one_in: int = 12
    proactive_cooldown: float = 12 * HOUR
    mute_duration: float = 72 * HOUR
    followup_window: float = 180.0
    buffer_capacity: int = DEFAULT_BUFFER_CAPACITY

    def __post_init__(self) -> None:
        for name, value in (
            ("sleep_start_hour", self.sleep_start_hour),
            ("sleep_end_hour", self.sleep_end_hour),
            ("proactive_start_hour", self.proactive_start_hour),
            ("proactive_end_hour", self.proactive_end_hour),
        ):
            if not 0 <= value <= 24:
                raise ValueError(f"{name} must be within 0..24")
        if self.proactive_start_hour > self.proactive_end_hour:
            raise ValueError("proactive window start must not exceed its end")
        if self.proactive_one_in < 1:
            raise ValueError("proactive_one_in must be >= 1")
        if self.proactive_cooldown < 0:
            raise ValueError("proactive_cooldown must be >= 0")
        if self.mute_duration <= 0:
            raise ValueError("mute_duration must be > 0")
        if self.followup_window <= 0:
            raise ValueError("followup_window must be > 0")
        if self.buffer_capacity < 1:
            raise ValueError("buffer_capacity must be >= 1")


@dataclass(frozen=True, slots=True)
class GateDecision:
    action: GateAction
    reason: GateReason


Clock = Callable[[], float]


class GroupGate:
    def __init__(
        self,
        *,
        scope: ScopeConfig,
        group: GroupConfig,
        bot_id: str,
        store: GroupStateStore,
        clock: Clock,
        rng: Rng,
        tz: tzinfo | None = None,
        policy: GatePolicy | None = None,
    ) -> None:
        self._scope = scope
        self._group = group
        self._bot_id = bot_id
        self._store = store
        self._clock = clock
        self._rng = rng
        self._tz = tz
        self._policy = policy if policy is not None else GatePolicy()

    async def decide(self, msg: InboundMessage) -> GateDecision:
        if msg.scope != "group" or msg.group_id is None:
            raise ValueError("GroupGate accepts group messages only")
        return self._decide(msg, msg.group_id)

    def note_bot_spoke(self, group_id: str, ts: float, message_id: str | None = None) -> None:
        """Open the follow-up window after a bot send and remember the sent message id."""
        state = self._store.get_state(group_id)
        self._store.put_state(replace(state, followup_until=ts + self._policy.followup_window))
        if message_id is not None:
            self._store.record_bot_message(message_id, group_id, ts)

    def note_other_reply(self, group_id: str, ts: float) -> bool:
        """Member reply: extend an open follow-up window; False if closed or during sleep."""
        if self._in_sleep(ts):
            return False
        state = self._store.get_state(group_id)
        if not followup_open(state.followup_until, ts):
            return False
        self._store.put_state(replace(state, followup_until=ts + self._policy.followup_window))
        return True

    def is_muted(self, group_id: str) -> bool:
        return is_muted(self._store.get_state(group_id).muted_until, self._clock())

    def recent_context(self, group_id: str) -> tuple[BufferedMessage, ...]:
        return self._store.buffer(group_id)

    def _decide(self, msg: InboundMessage, group_id: str) -> GateDecision:
        now = self._clock()
        if not self._scope.allows("group", msg.user_id, group_id):
            return GateDecision("ignore", "out_of_scope")
        if msg.user_id == self._bot_id:
            return GateDecision("ignore", "bot_self")
        if self._in_sleep(now):
            return GateDecision("ignore", "sleep")

        state = self._store.get_state(group_id)
        if is_muted(state.muted_until, now):
            return GateDecision("ignore", "muted")

        text = keyword_text(msg.segments)
        at_bot = mentions_user(msg.segments, self._bot_id)
        addressed = at_bot or self._reply_to_bot(msg, group_id)
        if is_mute_trigger(addressed, text, self._group.mute_keywords):
            self._store.put_state(
                replace(
                    state,
                    muted_until=mute_until(now, self._policy.mute_duration),
                    followup_until=None,
                )
            )
            self._store.clear_buffer(group_id)
            return GateDecision("mute_triggered", "mute_keyword")

        was_open = followup_open(state.followup_until, now)
        if was_open:
            self._store.put_state(replace(state, followup_until=now + self._policy.followup_window))
        if at_bot and contains_keyword(text, self._group.wake_keywords):
            return GateDecision("wake", "wake_keyword")
        if was_open:
            return GateDecision("followup", "member_reply")

        self._store.append_buffer(group_id, buffered_from(msg), self._policy.buffer_capacity)
        return self._roll(now, group_id, state)

    def _roll(self, now: float, group_id: str, state: GroupState) -> GateDecision:
        policy = self._policy
        result = evaluate_roll(
            now=now,
            local=self._local(now),
            last_roll_hour=state.last_roll_hour,
            last_proactive_at=state.last_proactive_at,
            rng=self._rng,
            start_hour=policy.proactive_start_hour,
            end_hour=policy.proactive_end_hour,
            one_in=policy.proactive_one_in,
            cooldown=policy.proactive_cooldown,
        )
        updated = state
        if result.roll_hour is not None:
            updated = replace(updated, last_roll_hour=result.roll_hour)
        if result.proactive_at is not None:
            updated = replace(updated, last_proactive_at=result.proactive_at)
        if updated != state:
            self._store.put_state(updated)
        if result.hit:
            return GateDecision("proactive", "roll_hit")
        return GateDecision("ignore", result.reason)

    def _reply_to_bot(self, msg: InboundMessage, group_id: str) -> bool:
        return msg.reply_to is not None and self._store.bot_message_group(msg.reply_to) == group_id

    def _in_sleep(self, now: float) -> bool:
        hour = self._local(now).hour
        return self._policy.sleep_start_hour <= hour < self._policy.sleep_end_hour

    def _local(self, now: float) -> datetime:
        return datetime.fromtimestamp(now, self._tz)
