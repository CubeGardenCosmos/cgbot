"""Offline tests: real websockets client <-> NapCatServer on 127.0.0.1 ephemeral port.

Covers R1.2-U01/U02 over the wire, plus handshake, auth, correlation, timeout, replacement.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import pytest
from websockets.asyncio.client import ClientConnection, connect
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosedOK, InvalidStatus

from cgbot.model import InboundMessage, InputStatus, OutboundMessage, Segment, SendResult
from cgbot.napcat import NapCatServer, NotConnectedError

SELF_ID = "10001"
TOKEN = "test-token-not-secret"
WAIT = 3.0


@dataclass
class Harness:
    server: NapCatServer
    messages: asyncio.Queue[InboundMessage] = field(default_factory=asyncio.Queue)
    statuses: asyncio.Queue[InputStatus] = field(default_factory=asyncio.Queue)
    disconnects: asyncio.Queue[str] = field(default_factory=asyncio.Queue)

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.server.bound_port}/ws"


@asynccontextmanager
async def make_harness(
    token: str | None = None, on_message_error: bool = False
) -> AsyncIterator[Harness]:
    holder: list[Harness] = []

    async def on_message(msg: InboundMessage) -> None:
        await holder[0].messages.put(msg)
        if on_message_error:
            raise RuntimeError("boom")

    async def on_status(status: InputStatus) -> None:
        await holder[0].statuses.put(status)

    async def on_disconnect(self_id: str) -> None:
        await holder[0].disconnects.put(self_id)

    server = NapCatServer(
        "127.0.0.1",
        0,
        path="/ws",
        token=token,
        on_message=on_message,
        on_input_status=on_status,
        on_disconnect=on_disconnect,
    )
    harness = Harness(server=server)
    holder.append(harness)
    await server.start()
    try:
        yield harness
    finally:
        await server.stop()


async def connect_napcat(
    url: str, self_id: str = SELF_ID, token: str | None = None
) -> ClientConnection:
    headers = {"x-self-id": self_id, "x-client-role": "Universal"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return await connect(url, additional_headers=Headers(headers))


async def wait_registered(
    server: NapCatServer, self_id: str = SELF_ID, previous: object = None
) -> None:
    """Poll until the server has registered a session for ``self_id`` other than ``previous``."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + WAIT
    while True:
        current = server._sessions.get(self_id)  # noqa: SLF001 - test-only registration check
        if current is not None and current is not previous:
            return
        if loop.time() > deadline:
            raise TimeoutError("session was not registered")
        await asyncio.sleep(0.01)


def private_message(message_id: int, text: str, quote: int | None = None) -> str:
    segments: list[dict[str, object]] = []
    if quote is not None:
        segments.append({"type": "reply", "data": {"id": str(quote)}})
    segments.append({"type": "text", "data": {"text": text}})
    return json.dumps(
        {
            "time": 1761800000,
            "self_id": int(SELF_ID),
            "post_type": "message",
            "message_type": "private",
            "sub_type": "friend",
            "message_id": message_id,
            "user_id": 20002,
            "message": segments,
            "raw_message": text,
        }
    )


async def next_action(ws: ClientConnection) -> dict[str, object]:
    raw = json.loads(await asyncio.wait_for(ws.recv(), WAIT))
    assert isinstance(raw, dict)
    return raw


async def reply_ok(ws: ClientConnection, action: dict[str, object], data: object) -> None:
    await ws.send(json.dumps({"status": "ok", "retcode": 0, "data": data, "echo": action["echo"]}))


async def test_handshake_lifecycle_then_private_message() -> None:
    async with make_harness() as h:
        ws = await connect_napcat(h.url)
        await ws.send(
            json.dumps(
                {
                    "time": 1761800000,
                    "self_id": int(SELF_ID),
                    "post_type": "meta_event",
                    "meta_event_type": "lifecycle",
                    "sub_type": "connect",
                }
            )
        )
        await ws.send(
            json.dumps(
                {
                    "time": 1761800030,
                    "self_id": int(SELF_ID),
                    "post_type": "meta_event",
                    "meta_event_type": "heartbeat",
                }
            )
        )
        await ws.send(private_message(555, "hello", quote=500))
        msg = await asyncio.wait_for(h.messages.get(), WAIT)
        assert msg.message_id == "555"
        assert msg.reply_to == "500"
        assert msg.user_id == "20002"
        assert msg.scope == "private"
        assert msg.segments == (
            Segment("reply", {"id": "500"}),
            Segment("text", {"text": "hello"}),
        )
        assert h.messages.empty()  # lifecycle and heartbeat produced no business event
        await ws.close()


async def test_input_status_delivered_including_empty_text() -> None:
    async with make_harness() as h:
        ws = await connect_napcat(h.url)
        base = {
            "time": 1761800100,
            "self_id": int(SELF_ID),
            "post_type": "notice",
            "notice_type": "notify",
            "sub_type": "input_status",
            "user_id": 20002,
            "group_id": 0,
        }
        await ws.send(json.dumps({**base, "event_type": 1, "status_text": "对方正在输入..."}))
        await ws.send(json.dumps({**base, "event_type": 2, "status_text": ""}))
        first = await asyncio.wait_for(h.statuses.get(), WAIT)
        second = await asyncio.wait_for(h.statuses.get(), WAIT)
        assert first == InputStatus("20002", 1, "对方正在输入...", 1761800100.0)
        assert second == InputStatus("20002", 2, "", 1761800100.0)
        await ws.close()


async def test_action_echo_correlation_out_of_order() -> None:
    async with make_harness() as h:
        ws = await connect_napcat(h.url)
        await wait_registered(h.server)

        async def client() -> None:
            first = await next_action(ws)
            second = await next_action(ws)
            assert first["action"] == "get_login_info"
            # answer in reverse order; each caller must still get its own response
            await reply_ok(ws, second, {"which": "second"})
            await reply_ok(ws, first, {"which": "first"})

        client_task = asyncio.create_task(client())
        a, b = await asyncio.gather(
            h.server.call("get_login_info", {"n": 1}, timeout_s=WAIT),
            h.server.call("get_login_info", {"n": 2}, timeout_s=WAIT),
        )
        await client_task
        assert a["data"] == {"which": "first"}
        assert b["data"] == {"which": "second"}
        assert a["echo"] != b["echo"]
        await ws.close()


async def test_send_private_message_success_returns_message_id() -> None:
    async with make_harness() as h:
        ws = await connect_napcat(h.url)
        await wait_registered(h.server)

        async def client() -> dict[str, object]:
            action = await next_action(ws)
            await reply_ok(ws, action, {"message_id": 98765})
            return action

        client_task = asyncio.create_task(client())
        result = await h.server.send(
            OutboundMessage("private", "20002", (Segment("text", {"text": "hi"}),), reply_to="555"),
            timeout_s=WAIT,
        )
        action = await client_task
        assert result == SendResult(ok=True, message_id="98765")
        assert action["action"] == "send_private_msg"
        assert action["params"] == {
            "user_id": 20002,
            "message": [
                {"type": "reply", "data": {"id": "555"}},
                {"type": "text", "data": {"text": "hi"}},
            ],
        }
        await ws.close()


async def test_send_group_failure_reported_not_raised() -> None:
    async with make_harness() as h:
        ws = await connect_napcat(h.url)
        await wait_registered(h.server)

        async def client() -> None:
            action = await next_action(ws)
            await ws.send(json.dumps({"status": "failed", "retcode": 1200, "echo": action["echo"]}))

        client_task = asyncio.create_task(client())
        result = await h.server.send(
            OutboundMessage("group", "30003", (Segment("text", {"text": "x"}),)), timeout_s=WAIT
        )
        await client_task
        assert result.ok is False
        assert result.error == "napcat status=failed retcode=1200"
        await ws.close()


async def test_send_timeout_returns_not_ok() -> None:
    async with make_harness() as h:
        ws = await connect_napcat(h.url)
        await wait_registered(h.server)
        result = await h.server.send(
            OutboundMessage("private", "20002", (Segment("text", {"text": "x"}),)), timeout_s=0.1
        )
        assert result.ok is False
        assert result.error == "timeout after 0.1s"
        with pytest.raises(TimeoutError):
            await h.server.call("get_login_info", {}, timeout_s=0.05)
        # the late response for a timed-out echo is ignored; the connection stays usable
        late = await next_action(ws)
        await reply_ok(ws, late, {})
        await ws.close()


async def test_send_without_connection_not_ok() -> None:
    async with make_harness() as h:
        result = await h.server.send(
            OutboundMessage("private", "20002", (Segment("text", {"text": "x"}),))
        )
        assert result.ok is False
        assert result.error is not None
        assert result.error.startswith("not connected")
        with pytest.raises(NotConnectedError):
            await h.server.call("get_login_info", {})


async def test_send_unexpressible_message_not_ok_without_network() -> None:
    async with make_harness() as h:
        result = await h.server.send(
            OutboundMessage("private", "20002", (Segment("forward", {"id": "1"}),))
        )
        assert result == SendResult(ok=False, error="unsupported outbound segment type: forward")


async def test_pending_call_fails_when_connection_drops() -> None:
    async with make_harness() as h:
        ws = await connect_napcat(h.url)
        await wait_registered(h.server)

        async def client() -> None:
            await next_action(ws)
            await ws.close()

        client_task = asyncio.create_task(client())
        result = await h.server.send(
            OutboundMessage("private", "20002", (Segment("text", {"text": "x"}),)), timeout_s=WAIT
        )
        await client_task
        assert result.ok is False
        assert result.error is not None
        assert result.error.startswith("not connected")
        assert await asyncio.wait_for(h.disconnects.get(), WAIT) == SELF_ID


async def test_handler_can_send_reply_without_deadlock() -> None:
    """on_message awaits an action response: the receive loop must keep reading."""
    holder: list[NapCatServer] = []
    replies: asyncio.Queue[SendResult] = asyncio.Queue()

    async def on_message(msg: InboundMessage) -> None:
        result = await holder[0].send(
            OutboundMessage(
                "private",
                msg.user_id,
                (Segment("text", {"text": "ack"}),),
                reply_to=msg.message_id,
            ),
            timeout_s=WAIT,
        )
        await replies.put(result)

    async def on_status(_: InputStatus) -> None:
        return None

    server = NapCatServer("127.0.0.1", 0, on_message=on_message, on_input_status=on_status)
    holder.append(server)
    await server.start()
    try:
        ws = await connect_napcat(f"ws://127.0.0.1:{server.bound_port}/ws")
        await ws.send(private_message(77, "ping"))
        action = await next_action(ws)
        assert action["action"] == "send_private_msg"
        await reply_ok(ws, action, {"message_id": 78})
        result = await asyncio.wait_for(replies.get(), WAIT)
        assert result == SendResult(ok=True, message_id="78")
        await ws.close()
    finally:
        await server.stop()


async def test_failing_handler_does_not_kill_connection() -> None:
    async with make_harness(on_message_error=True) as h:
        ws = await connect_napcat(h.url)
        await ws.send(private_message(1, "a"))
        await ws.send(private_message(2, "b"))
        first = await asyncio.wait_for(h.messages.get(), WAIT)
        second = await asyncio.wait_for(h.messages.get(), WAIT)
        assert (first.message_id, second.message_id) == ("1", "2")
        await ws.close()


async def test_token_required_and_checked() -> None:
    async with make_harness(token=TOKEN) as h:
        with pytest.raises(InvalidStatus) as missing:
            await connect_napcat(h.url)
        assert missing.value.response.status_code == 401

        with pytest.raises(InvalidStatus) as wrong:
            await connect_napcat(h.url, token="wrong")
        assert wrong.value.response.status_code == 401

        ws = await connect_napcat(h.url, token=TOKEN)
        await ws.send(private_message(9, "ok"))
        assert (await asyncio.wait_for(h.messages.get(), WAIT)).message_id == "9"
        await ws.close()


async def test_no_token_configured_accepts_without_authorization() -> None:
    async with make_harness(token=None) as h:
        ws = await connect_napcat(h.url)
        await wait_registered(h.server)
        await ws.close()


async def test_wrong_path_and_missing_self_id_rejected() -> None:
    async with make_harness() as h:
        with pytest.raises(InvalidStatus) as wrong_path:
            await connect(
                f"ws://127.0.0.1:{h.server.bound_port}/other",
                additional_headers=Headers({"x-self-id": SELF_ID}),
            )
        assert wrong_path.value.response.status_code == 404
        with pytest.raises(InvalidStatus) as no_self:
            await connect(h.url)
        assert no_self.value.response.status_code == 400


async def test_new_connection_replaces_old_for_same_self_id() -> None:
    async with make_harness() as h:
        old = await connect_napcat(h.url)
        await wait_registered(h.server)
        previous = h.server._sessions[SELF_ID]  # noqa: SLF001
        new = await connect_napcat(h.url)
        await wait_registered(h.server, previous=previous)

        with pytest.raises(ConnectionClosedOK):
            await asyncio.wait_for(old.recv(), WAIT)
        assert await asyncio.wait_for(h.disconnects.get(), WAIT) == SELF_ID

        async def client() -> dict[str, object]:
            action = await next_action(new)
            await reply_ok(new, action, {"message_id": 1})
            return action

        client_task = asyncio.create_task(client())
        result = await h.server.send(
            OutboundMessage("private", "20002", (Segment("text", {"text": "x"}),)), timeout_s=WAIT
        )
        await client_task
        assert result.ok is True
        await new.close()


async def test_disconnect_callback_invoked_when_client_closes() -> None:
    async with make_harness() as h:
        ws = await connect_napcat(h.url, self_id="10002")
        await wait_registered(h.server, self_id="10002")
        await ws.close()
        assert await asyncio.wait_for(h.disconnects.get(), WAIT) == "10002"


async def test_stop_closes_connections_and_is_idempotent() -> None:
    async with make_harness() as h:
        ws = await connect_napcat(h.url)
        await wait_registered(h.server)
        await h.server.stop()
        with pytest.raises(ConnectionClosedOK):
            await asyncio.wait_for(ws.recv(), WAIT)
        await h.server.stop()
