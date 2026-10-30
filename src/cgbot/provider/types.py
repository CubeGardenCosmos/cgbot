"""Provider-neutral domain types for the single upstream model (R1.5).

The agent loop (R1.6) and compaction (R2.3) depend only on these types. The OpenAI Responses
wire format is confined to ``responses.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class ModelRequest:
    """One model turn. ``input`` holds Responses input items (see ``items.py`` helpers).

    ``reasoning_effort`` is passed through verbatim to the upstream; mapping purposes to tiers
    belongs to configuration, not to this layer.
    """

    instructions: str
    input: list[dict[str, Any]]
    tools: list[dict[str, Any]] = field(default_factory=list)
    reasoning_effort: str | None = None
    max_output_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class TextOutput:
    """Assistant text (output message). ``raw`` is the wire item, kept for replay."""

    text: str
    raw: Mapping[str, Any] = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class FunctionCall:
    """A tool call. ``arguments`` is the raw JSON text; ``parsed_arguments`` is None iff
    ``parse_error`` is set (invalid JSON or not a JSON object). Empty arguments parse as ``{}``.
    """

    call_id: str
    name: str
    arguments: str
    parsed_arguments: Mapping[str, Any] | None
    parse_error: str | None
    raw: Mapping[str, Any] = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class Reasoning:
    """Reasoning item. ``summary`` is the joined summary text; ``raw`` keeps encrypted content."""

    summary: str
    raw: Mapping[str, Any] = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class OtherOutput:
    """Any output item type not modelled above (e.g. built-in tool calls), kept verbatim."""

    type: str
    raw: Mapping[str, Any] = field(repr=False, compare=False)


OutputItem = TextOutput | FunctionCall | Reasoning | OtherOutput


@dataclass(frozen=True, slots=True)
class Usage:
    """Token usage as reported by the upstream; missing counters are 0."""

    input_tokens: int = 0
    output_tokens: int = 0
    cached: int = 0
    reasoning: int = 0


@dataclass(frozen=True, slots=True)
class ModelResponse:
    """A completed (or incomplete) model turn. Failed turns raise instead of returning."""

    items: tuple[OutputItem, ...]
    usage: Usage
    response_id: str
    status: str
    incomplete_reason: str | None = None


@dataclass(frozen=True, slots=True)
class TextDelta:
    text: str


@dataclass(frozen=True, slots=True)
class ItemDone:
    """A fully received output item (tool calls are only complete at this point)."""

    item: OutputItem


@dataclass(frozen=True, slots=True)
class Completed:
    """Terminal event of a successful stream; always the last event yielded."""

    response: ModelResponse


StreamEvent = TextDelta | ItemDone | Completed


class ModelProvider(Protocol):
    """Interface the agent loop uses for the single upstream model."""

    @property
    def name(self) -> str: ...

    @property
    def context_window(self) -> int: ...

    async def respond(self, request: ModelRequest) -> ModelResponse: ...

    def respond_stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]: ...
