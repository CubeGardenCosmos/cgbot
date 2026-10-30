"""Notification normalisation into CodexEvent, and the event stream lifecycle."""

from __future__ import annotations

import asyncio

import pytest
from appserver_fakes import TOKEN, FakeCodex

from cgbot.coding.appserver import CodexAppServerClient, connect_codex
from cgbot.coding.contracts import CodexEvent, SandboxEndpoint


@pytest.fixture
async def fake():
    server = FakeCodex()
    await server.start()
    yield server
    await server.stop()


async def open_client(fake: FakeCodex) -> CodexAppServerClient:
    endpoint = SandboxEndpoint(user_id="u1", container="c1", url=fake.url, ws_token=TOKEN)
    return await connect_codex(endpoint, request_timeout=5.0)


async def next_events(client: CodexAppServerClient, count: int) -> list[CodexEvent]:
    out: list[CodexEvent] = []
    stream = client.events()
    async with asyncio.timeout(2.0):
        async for event in stream:
            out.append(event)
            if len(out) == count:
                break
    return out


async def test_turn_started_and_agent_message_are_normalised(fake: FakeCodex) -> None:
    client = await open_client(fake)
    try:
        await fake.push(
            {"method": "turn/started", "params": {"threadId": "t1", "turn": {"id": "u1"}}}
        )
        await fake.push(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "t1",
                    "turnId": "u1",
                    "item": {"type": "agentMessage", "id": "i1", "text": "PONG"},
                },
            }
        )
        started, item = await next_events(client, 2)
        assert started.kind == "turn_started"
        assert (started.thread_id, started.turn_id) == ("t1", "u1")
        assert item.kind == "item_completed"
        assert item.text == "PONG"
        assert item.params["item"] == {"type": "agentMessage", "id": "i1", "text": "PONG"}
    finally:
        await client.close()


async def test_turn_completed_uses_streamed_text_then_turn_items(fake: FakeCodex) -> None:
    client = await open_client(fake)
    try:
        await fake.push(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "t1",
                    "turnId": "u1",
                    "item": {"type": "agentMessage", "id": "i1", "text": "from item"},
                },
            }
        )
        await fake.push(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "t1",
                    "turn": {"id": "u1", "status": "completed", "items": []},
                },
            }
        )
        await fake.push(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "t1",
                    "turn": {
                        "id": "u2",
                        "status": "completed",
                        "items": [
                            {"type": "userMessage", "text": "q"},
                            {"type": "agentMessage", "text": "last one"},
                        ],
                    },
                },
            }
        )
        _, done1, done2 = await next_events(client, 3)
        assert (done1.kind, done1.status, done1.turn_id, done1.text) == (
            "turn_completed",
            "completed",
            "u1",
            "from item",
        )
        assert (done2.turn_id, done2.text) == ("u2", "last one")
    finally:
        await client.close()


@pytest.mark.parametrize("status", ["interrupted", "failed"])
async def test_turn_completed_keeps_terminal_statuses(fake: FakeCodex, status: str) -> None:
    client = await open_client(fake)
    try:
        await fake.push(
            {
                "method": "turn/completed",
                "params": {"threadId": "t1", "turn": {"id": "u1", "status": status, "items": []}},
            }
        )
        (event,) = await next_events(client, 1)
        assert event.status == status
        assert event.text is None
    finally:
        await client.close()


async def test_in_progress_turn_completed_is_not_emitted(fake: FakeCodex) -> None:
    client = await open_client(fake)
    try:
        await fake.push(
            {
                "method": "turn/completed",
                "params": {"threadId": "t1", "turn": {"id": "u1", "status": "inProgress"}},
            }
        )
        await fake.push(
            {"method": "turn/started", "params": {"threadId": "t1", "turn": {"id": "u2"}}}
        )
        (event,) = await next_events(client, 1)
        assert event.kind == "turn_started"
        assert event.turn_id == "u2"
    finally:
        await client.close()


async def test_token_usage_status_and_error_events(fake: FakeCodex) -> None:
    client = await open_client(fake)
    try:
        await fake.push(
            {
                "method": "thread/tokenUsage/updated",
                "params": {"threadId": "t1", "turnId": "u1", "tokenUsage": {"total": {}}},
            }
        )
        await fake.push(
            {
                "method": "thread/status/changed",
                "params": {"threadId": "t1", "status": {"type": "active", "activeFlags": []}},
            }
        )
        await fake.push(
            {
                "method": "error",
                "params": {
                    "threadId": "t1",
                    "turnId": "u1",
                    "willRetry": True,
                    "error": {"message": "stream dropped"},
                },
            }
        )
        usage, status, error = await next_events(client, 3)
        assert usage.kind == "token_usage"
        assert usage.params["tokenUsage"] == {"total": {}}
        assert (status.kind, status.status, status.thread_id) == ("thread_status", "active", "t1")
        assert (error.kind, error.text, error.turn_id) == ("error", "stream dropped", "u1")
        assert error.params["willRetry"] is True
    finally:
        await client.close()


async def test_unmapped_notifications_are_dropped(fake: FakeCodex) -> None:
    client = await open_client(fake)
    try:
        await fake.push({"method": "item/agentMessage/delta", "params": {"delta": "P"}})
        await fake.push({"method": "account/rateLimits/updated", "params": {}})
        await fake.push(
            {"method": "turn/started", "params": {"threadId": "t1", "turn": {"id": "u1"}}}
        )
        (event,) = await next_events(client, 1)
        assert event.kind == "turn_started"
    finally:
        await client.close()


async def test_event_stream_ends_when_connection_closes(fake: FakeCodex) -> None:
    client = await open_client(fake)
    await fake.push({"method": "turn/started", "params": {"threadId": "t1", "turn": {"id": "u1"}}})
    await fake.drop()
    events = [event async for event in client.events()]
    assert [e.kind for e in events] == ["turn_started"]
    await client.close()
