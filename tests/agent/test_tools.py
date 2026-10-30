from __future__ import annotations

from typing import Any

import pytest

from cgbot.agent import (
    REPLY_TOOL_NAME,
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    builtin_tools,
)
from cgbot.agent.tools import available_channels, channel_key
from cgbot.model import Channel


async def _noop(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    return ToolResult("ok")


def _spec(name: str = "t") -> ToolSpec:
    return ToolSpec(name, "desc", {"type": "object", "properties": {}}, _noop)


def test_duplicate_registration_is_rejected() -> None:
    registry = ToolRegistry()
    registry.register(_spec("t"))
    with pytest.raises(ValueError):
        registry.register(_spec("t"))


def test_channel_param_must_be_schema_property() -> None:
    registry = ToolRegistry()
    bad = ToolSpec(
        "t", "desc", {"type": "object", "properties": {}}, _noop, channel_param="channel"
    )
    with pytest.raises(ValueError):
        registry.register(bad)


def test_get_returns_none_for_unknown() -> None:
    assert ToolRegistry().get("missing") is None


def test_schemas_are_responses_function_tools() -> None:
    registry = ToolRegistry()
    registry.register(_spec("t"))
    [schema] = registry.schemas()
    assert schema["type"] == "function"
    assert schema["name"] == "t"
    assert schema["strict"] is False
    assert schema["parameters"] == {"type": "object", "properties": {}}


def test_builtin_reply_schema_has_channel_enum_and_no_identity_args() -> None:
    registry = builtin_tools()
    channels = (Channel("qq", "10001"), Channel("qq", "20002"))
    [schema] = registry.schemas(channels)
    assert schema["name"] == REPLY_TOOL_NAME
    params = schema["parameters"]
    assert params["additionalProperties"] is False
    assert "user_id" not in params["properties"]
    assert params["properties"]["channel"]["enum"] == ["qq:10001", "qq:20002"]
    assert params["required"] == ["text"]


def test_schema_enum_is_not_mutated_on_shared_spec() -> None:
    registry = builtin_tools()
    registry.schemas((Channel("qq", "1"),))
    [schema] = registry.schemas()
    assert "enum" not in schema["parameters"]["properties"]["channel"]


def test_available_channels_puts_source_first_and_dedupes() -> None:
    source = Channel("qq", "1")
    other = Channel("qq", "2")
    assert available_channels(source, [other, source, other]) == (source, other)
    assert channel_key(other) == "qq:2"
