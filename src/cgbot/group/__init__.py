"""Group chat subsystem (M3). Gate side: gating, proactive roll, mute, follow-up, buffer."""

from cgbot.group.buffer import DEFAULT_BUFFER_CAPACITY, BufferedMessage
from cgbot.group.gate import GateAction, GateDecision, GatePolicy, GateReason, GroupGate
from cgbot.group.session import GroupSessionManager, GroupSessionPolicy, GroupTrigger
from cgbot.group.skill import (
    GroupSkillStore,
    InMemoryProfileSink,
    ProfileEntry,
    ProfileSink,
    SkillCard,
    SkillLimits,
)
from cgbot.group.store import GroupState, GroupStateStore
from cgbot.group.tools import (
    END_CONVERSATION_TOOL_NAME,
    GROUP_REPLY_TOOL_NAME,
    TurnControl,
    group_tools,
)

__all__ = [
    "DEFAULT_BUFFER_CAPACITY",
    "BufferedMessage",
    "GateAction",
    "GateDecision",
    "GatePolicy",
    "GateReason",
    "GroupGate",
    "GroupState",
    "GroupStateStore",
    "END_CONVERSATION_TOOL_NAME",
    "GROUP_REPLY_TOOL_NAME",
    "GroupSessionManager",
    "GroupSessionPolicy",
    "GroupSkillStore",
    "GroupTrigger",
    "InMemoryProfileSink",
    "ProfileEntry",
    "ProfileSink",
    "SkillCard",
    "SkillLimits",
    "TurnControl",
    "group_tools",
]
