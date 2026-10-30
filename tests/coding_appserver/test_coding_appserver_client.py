"""Client behaviour against a scripted app-server: handshake, correlation, errors, requests."""

from __future__ import annotations

import asyncio
import logging
import socket

import pytest
from appserver_fakes import NO_REPLY, TOKEN, FakeCodex, RpcFail

from cgbot.coding.appserver import (
    CodexAppServerClient,
    CodexConnectionError,
    CodexProtocolError,
    CodexRpcError,
    CodexTimeoutError,
    connect_codex,
)
from cgbot.coding.contracts import SandboxEndpoint


@pytest.fixture
async def fake():
    server = FakeCodex()
    await server.start()
    yield server
    await server.stop()


def endpoint(fake: FakeCodex, *, token: str = TOKEN) -> SandboxEndpoint:
    return SandboxEndpoint(user_id="u1", container="c1", url=fake.url, ws_token=token)


async def open_client(fake: FakeCodex, *, request_timeout: float = 5.0) -> CodexAppServerClient:
    return await connect_codex(endpoint(fake), request_timeout=request_timeout)


async def wait_until(predicate, attempts: int = 200) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


def unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def test_handshake_sends_bearer_and_initialized(fake: FakeCodex) -> None:
    client = await open_client(fake)
    try:
        assert fake.handshake_headers[0]["Authorization"] == f"Bearer {TOKEN}"
        assert fake.calls[0][0] == "initialize"
        init = fake.calls[0][1]
        assert init["clientInfo"]["name"] == "cgbot"
        assert init["capabilities"] == {"experimentalApi": False}
        await wait_until(lambda: len(fake.calls) >= 2)
        assert fake.calls[1][0] == "initialized"
    finally:
        await client.close()


async def test_wrong_token_is_rejected_without_leaking_it(
    fake: FakeCodex, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "wrong-secret-value-xyz"
    # Only cgbot's own logger is checked: the websockets library logs raw headers at DEBUG,
    # so production logging must keep that logger above DEBUG (see the notes doc).
    caplog.set_level(logging.DEBUG, logger="cgbot")
    with pytest.raises(CodexConnectionError) as info:
        await connect_codex(endpoint(fake, token=secret))
    assert "401" in str(info.value)
    assert secret not in str(info.value)
    assert secret not in caplog.text


async def test_unreachable_endpoint_raises_connection_error() -> None:
    url = f"ws://127.0.0.1:{unused_port()}"
    with pytest.raises(CodexConnectionError):
        await connect_codex(
            SandboxEndpoint(user_id="u1", container="c1", url=url, ws_token=TOKEN),
            open_timeout=2.0,
        )


async def test_responses_are_matched_by_id_when_answered_out_of_order(fake: FakeCodex) -> None:
    release_first = asyncio.Event()

    async def slow_list(_params: dict) -> dict:
        await release_first.wait()
        return {"data": [], "nextCursor": None}

    async def fast_read(_params: dict) -> dict:
        release_first.set()
        return {"data": [], "nextCursor": None}

    fake.handlers["thread/list"] = slow_list
    fake.handlers["thread/turns/list"] = fast_read
    client = await open_client(fake)
    try:
        listed, read = await asyncio.gather(client.thread_list(), client.thread_read("t1"))
        assert listed == []
        assert read == ([], None)
    finally:
        await client.close()


async def test_notifications_before_response_do_not_resolve_requests(fake: FakeCodex) -> None:
    async def start(_params: dict) -> dict:
        await fake.push({"method": "thread/status/changed", "params": {"threadId": "t1"}})
        return {
            "thread": {"id": "t1", "updatedAt": 1700000000, "name": None},
        }

    fake.handlers["thread/start"] = start
    client = await open_client(fake)
    try:
        info = await client.thread_start()
        assert info.thread_id == "t1"
        assert info.updated_at == 1700000000.0
        assert info.title is None
        assert info.archived is False
    finally:
        await client.close()


async def test_json_rpc_error_surfaces_code_and_message(fake: FakeCodex) -> None:
    def fail(_params: dict) -> dict:
        raise RpcFail(-32000, "thread not found")

    fake.handlers["thread/archive"] = fail
    client = await open_client(fake)
    try:
        with pytest.raises(CodexRpcError) as info:
            await client.thread_archive("missing")
        assert info.value.method == "thread/archive"
        assert info.value.code == -32000
        assert "thread not found" in str(info.value)
    finally:
        await client.close()


async def test_unanswered_request_times_out_and_client_stays_usable(fake: FakeCodex) -> None:
    fake.handlers["thread/delete"] = lambda _p: NO_REPLY
    client = await open_client(fake, request_timeout=0.2)
    try:
        with pytest.raises(CodexTimeoutError):
            await client.thread_delete("t1")
        fake.handlers["thread/delete"] = lambda _p: {}
        await client.thread_delete("t1")
        assert fake.params_of("thread/delete")[-1] == {"threadId": "t1"}
    finally:
        await client.close()


async def test_connection_loss_fails_pending_requests_and_ends_events(fake: FakeCodex) -> None:
    fake.handlers["turn/interrupt"] = lambda _p: NO_REPLY
    client = await open_client(fake, request_timeout=5.0)
    pending = asyncio.create_task(client.turn_interrupt("t1", "u1"))
    await wait_until(lambda: "turn/interrupt" in fake.methods())

    await fake.drop()

    with pytest.raises(CodexConnectionError):
        await asyncio.wait_for(pending, 2.0)
    seen = [event async for event in client.events()]
    assert seen == []
    with pytest.raises(CodexConnectionError):
        await client.thread_rename("t1", "x")
    await client.close()


async def test_malformed_frame_closes_connection(fake: FakeCodex) -> None:
    client = await open_client(fake)
    await fake.push({"no": "shape"})  # neither method nor id
    seen = [event async for event in client.events()]
    assert seen == []
    with pytest.raises(CodexConnectionError):
        await client.thread_rename("t1", "x")
    await client.close()


async def test_server_command_approval_is_accepted(fake: FakeCodex) -> None:
    client = await open_client(fake)
    try:
        await fake.push(
            {
                "id": 77,
                "method": "item/commandExecution/requestApproval",
                "params": {"threadId": "t1", "turnId": "u1", "itemId": "i1"},
            }
        )
        await wait_until(lambda: bool(fake.answers))
        assert fake.answers[0] == {"id": 77, "result": {"decision": "accept"}}
    finally:
        await client.close()


async def test_server_user_input_request_gets_empty_answers(fake: FakeCodex) -> None:
    client = await open_client(fake)
    try:
        await fake.push(
            {
                "id": 5,
                "method": "item/tool/requestUserInput",
                "params": {
                    "threadId": "t1",
                    "turnId": "u1",
                    "itemId": "i1",
                    "questions": [{"id": "q1"}, {"id": "q2"}],
                },
            }
        )
        await wait_until(lambda: bool(fake.answers))
        assert fake.answers[0] == {
            "id": 5,
            "result": {"answers": {"q1": {"answers": []}, "q2": {"answers": []}}},
        }
    finally:
        await client.close()


async def test_unknown_server_request_gets_method_not_found(fake: FakeCodex) -> None:
    client = await open_client(fake)
    try:
        await fake.push({"id": 9, "method": "account/chatgptAuthTokens/refresh", "params": {}})
        await wait_until(lambda: bool(fake.answers))
        assert fake.answers[0]["id"] == 9
        assert fake.answers[0]["error"]["code"] == -32601
    finally:
        await client.close()


async def test_thread_start_with_title_renames_and_returns_title(fake: FakeCodex) -> None:
    fake.handlers["thread/start"] = lambda _p: {
        "thread": {"id": "t9", "updatedAt": 1700000100, "name": None}
    }
    fake.handlers["thread/name/set"] = lambda _p: {}
    client = await open_client(fake)
    try:
        info = await client.thread_start(title="demo")
        assert fake.params_of("thread/name/set") == [{"threadId": "t9", "name": "demo"}]
        assert info.title == "demo"
        assert info.updated_at == 1700000100.0
    finally:
        await client.close()


async def test_thread_list_pages_and_include_archived_takes_two_filters(fake: FakeCodex) -> None:
    def list_threads(params: dict) -> dict:
        if params["archived"] is False and "cursor" not in params:
            return {
                "data": [{"id": "a", "updatedAt": 10, "name": "A"}],
                "nextCursor": "c1",
            }
        if params["archived"] is False:
            return {"data": [{"id": "b", "updatedAt": 20}], "nextCursor": None}
        return {"data": [{"id": "z", "updatedAt": 30}], "nextCursor": None}

    fake.handlers["thread/list"] = list_threads
    client = await open_client(fake)
    try:
        active = await client.thread_list()
        assert [t.thread_id for t in active] == ["a", "b"]
        assert active[0].title == "A"
        assert all(t.archived is False for t in active)

        everything = await client.thread_list(include_archived=True)
        assert [(t.thread_id, t.archived) for t in everything] == [
            ("a", False),
            ("b", False),
            ("z", True),
        ]
        assert fake.params_of("thread/list")[1]["cursor"] == "c1"
    finally:
        await client.close()


async def test_thread_read_requests_oldest_first_with_full_items(fake: FakeCodex) -> None:
    turns = [{"id": "u1", "items": [{"type": "userMessage"}]}]
    fake.handlers["thread/turns/list"] = lambda _p: {"data": turns, "nextCursor": "next"}
    client = await open_client(fake)
    try:
        got, cursor = await client.thread_read("t1", cursor="c0", limit=5)
        assert got == turns
        assert cursor == "next"
        sent = fake.params_of("thread/turns/list")[0]
        assert sent == {
            "threadId": "t1",
            "limit": 5,
            "sortDirection": "asc",
            "itemsView": "full",
            "cursor": "c0",
        }
    finally:
        await client.close()


async def test_turn_start_sends_text_input_and_returns_turn_id(fake: FakeCodex) -> None:
    fake.handlers["turn/start"] = lambda _p: {"turn": {"id": "turn-1", "status": "inProgress"}}
    client = await open_client(fake)
    try:
        turn_id = await client.turn_start("t1", "hello")
        assert turn_id == "turn-1"
        assert fake.params_of("turn/start")[0] == {
            "threadId": "t1",
            "input": [{"type": "text", "text": "hello"}],
        }
    finally:
        await client.close()


async def test_turn_start_without_turn_id_is_protocol_error(fake: FakeCodex) -> None:
    fake.handlers["turn/start"] = lambda _p: {"turn": {}}
    client = await open_client(fake)
    try:
        with pytest.raises(CodexProtocolError):
            await client.turn_start("t1", "hello")
    finally:
        await client.close()


async def test_turn_steer_sends_expected_turn_id(fake: FakeCodex) -> None:
    fake.handlers["turn/steer"] = lambda _p: {"turnId": "turn-1"}
    client = await open_client(fake)
    try:
        await client.turn_steer("t1", "turn-1", "also this")
        assert fake.params_of("turn/steer")[0] == {
            "threadId": "t1",
            "expectedTurnId": "turn-1",
            "input": [{"type": "text", "text": "also this"}],
        }
    finally:
        await client.close()


@pytest.mark.parametrize(
    ("call", "method", "params"),
    [
        (lambda c: c.thread_resume("t1"), "thread/resume", {"threadId": "t1"}),
        (lambda c: c.thread_fork("t1"), "thread/fork", {"threadId": "t1"}),
        (lambda c: c.thread_archive("t1"), "thread/archive", {"threadId": "t1"}),
        (lambda c: c.thread_delete("t1"), "thread/delete", {"threadId": "t1"}),
        (lambda c: c.thread_compact("t1"), "thread/compact/start", {"threadId": "t1"}),
        (
            lambda c: c.thread_rename("t1", "New"),
            "thread/name/set",
            {"threadId": "t1", "name": "New"},
        ),
        (
            lambda c: c.turn_interrupt("t1", "u1"),
            "turn/interrupt",
            {"threadId": "t1", "turnId": "u1"},
        ),
    ],
)
async def test_simple_methods_send_schema_names(
    fake: FakeCodex, call, method: str, params: dict
) -> None:
    fake.handlers.update(
        {
            "thread/resume": lambda _p: {"thread": {"id": "t1", "updatedAt": 1}},
            "thread/fork": lambda _p: {"thread": {"id": "t2", "updatedAt": 2}},
        }
    )
    for name in (
        "thread/archive",
        "thread/delete",
        "thread/compact/start",
        "thread/name/set",
        "turn/interrupt",
    ):
        fake.handlers.setdefault(name, lambda _p: {})
    client = await open_client(fake)
    try:
        await call(client)
        assert fake.params_of(method) == [params]
    finally:
        await client.close()


async def test_close_is_idempotent_and_blocks_further_requests(fake: FakeCodex) -> None:
    client = await open_client(fake)
    await client.close()
    await client.close()
    with pytest.raises(CodexConnectionError):
        await client.thread_archive("t1")


async def test_unexpected_notification_body_is_not_logged(
    fake: FakeCodex, caplog: pytest.LogCaptureFixture
) -> None:
    client = await open_client(fake)
    caplog.set_level(logging.DEBUG)
    await fake.push({"method": "error", "params": {"error": {"message": "sensitive-body-xyz"}}})
    await asyncio.sleep(0.05)
    assert "sensitive-body-xyz" not in caplog.text
    await client.close()
