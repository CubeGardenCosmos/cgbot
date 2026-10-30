"""OmpClient / helpers: auth detection, relogin, error mapping, redaction, SSE parsing."""

from __future__ import annotations

import httpx
import pytest
from omp_fakes import FakeOmpweb

from cgbot.omp.client import (
    MSG_CONN_REFUSED,
    MSG_CONN_TIMEOUT,
    MSG_LOGIN_REJECTED,
    MSG_OP_FAILED,
    MSG_REQUEST_TIMEOUT,
    MSG_STREAM_UNAVAILABLE,
    MSG_UNSUPPORTED,
    ClientPool,
    OmpClient,
    OmpError,
    base_url,
    redact,
    validate_alias,
    validate_host,
    validate_port,
)
from cgbot.omp.store import Machine

PASSWORD = "pw-secret-123"


def make_machine(password: str = PASSWORD) -> Machine:
    return Machine(
        owner="u1",
        alias="box",
        host="127.0.0.1",
        port=8080,
        password=password,
        auth="cookie",
        added_at=1.0,
    )


def make_client(fake: FakeOmpweb, password: str = PASSWORD) -> OmpClient:
    return OmpClient(machine=make_machine(password), http=fake.factory()(), passwords=[])


async def test_cookie_server_connects_and_logs_in() -> None:
    fake = FakeOmpweb()
    client = make_client(fake)
    assert await client.connect() == "cookie"
    assert client.auth == "cookie"
    assert fake.logged_in is True
    await client.aclose()


async def test_no_auth_server_connects_without_login() -> None:
    fake = FakeOmpweb(auth="none")
    client = make_client(fake)
    assert await client.connect() == "none"
    assert client.auth == "none"
    await client.aclose()


async def test_basic_auth_server_is_unsupported() -> None:
    fake = FakeOmpweb(auth="basic")
    client = make_client(fake)
    with pytest.raises(OmpError) as info:
        await client.connect()
    assert info.value.message == MSG_UNSUPPORTED
    await client.aclose()


async def test_wrong_password_is_rejected_without_echoing_it() -> None:
    fake = FakeOmpweb()
    client = make_client(fake, password="wrong-pw-999")
    with pytest.raises(OmpError) as info:
        await client.connect()
    assert info.value.message == MSG_LOGIN_REJECTED
    assert "wrong-pw-999" not in info.value.message
    await client.aclose()


async def test_login_404_means_unsupported_server() -> None:
    fake = FakeOmpweb()
    fake.fail_path["/api/web-auth/session"] = httpx.Response(404)
    client = make_client(fake)
    with pytest.raises(OmpError) as info:
        await client.connect()
    assert info.value.message == MSG_UNSUPPORTED
    await client.aclose()


async def test_relogs_in_once_after_session_expiry() -> None:
    fake = FakeOmpweb()
    client = make_client(fake)
    await client.connect()
    logins_before = sum(1 for r in fake.requests if r[1] == "/api/web-auth/session")
    fake.expire()
    data = await client.get("/api/sessions")
    assert isinstance(data["sessions"], list)
    logins_after = sum(1 for r in fake.requests if r[1] == "/api/web-auth/session")
    assert logins_after == logins_before + 1
    await client.aclose()


async def test_success_false_maps_to_operation_failed() -> None:
    fake = FakeOmpweb()
    client = make_client(fake)
    await client.connect()
    fake.fail_path["/api/sessions"] = httpx.Response(200, json={"success": False})
    with pytest.raises(OmpError) as info:
        await client.get("/api/sessions")
    assert info.value.message == MSG_OP_FAILED
    await client.aclose()


async def test_http_error_body_is_scrubbed_of_password() -> None:
    fake = FakeOmpweb()
    client = make_client(fake)
    await client.connect()
    fake.fail_path["/api/sessions"] = httpx.Response(
        500, json={"error": f"internal saw {PASSWORD} here"}
    )
    with pytest.raises(OmpError) as info:
        await client.get("/api/sessions")
    assert PASSWORD not in info.value.message
    assert "HTTP 500" in info.value.message
    await client.aclose()


async def test_connection_refused_is_mapped() -> None:
    fake = FakeOmpweb()
    fake.fail_path["/api/home"] = httpx.ConnectError("Connection refused")
    client = make_client(fake)
    with pytest.raises(OmpError) as info:
        await client.connect()
    assert info.value.message == MSG_CONN_REFUSED
    await client.aclose()


async def test_connect_timeout_is_mapped() -> None:
    fake = FakeOmpweb()
    fake.fail_path["/api/home"] = httpx.ConnectTimeout("slow connect")
    client = make_client(fake)
    with pytest.raises(OmpError) as info:
        await client.connect()
    assert info.value.message == MSG_CONN_TIMEOUT
    await client.aclose()


async def test_read_timeout_after_connect_is_request_timeout() -> None:
    fake = FakeOmpweb()
    client = make_client(fake)
    await client.connect()
    fake.fail_path["/api/sessions"] = httpx.ReadTimeout("slow read")
    with pytest.raises(OmpError) as info:
        await client.get("/api/sessions")
    assert info.value.message == MSG_REQUEST_TIMEOUT
    await client.aclose()


async def test_events_yield_json_objects_in_order() -> None:
    fake = FakeOmpweb()
    session = fake.add_session("s1", cwd="/w")
    session.events = [{"type": "agent_start"}, {"type": "session_settled"}]
    client = make_client(fake)
    await client.connect()
    received = [event async for event in client.events("s1")]
    assert received == [{"type": "agent_start"}, {"type": "session_settled"}]
    await client.aclose()


async def test_events_for_unknown_session_are_stream_unavailable() -> None:
    fake = FakeOmpweb()
    client = make_client(fake)
    await client.connect()
    with pytest.raises(OmpError) as info:
        _ = [event async for event in client.events("missing")]
    assert info.value.message == MSG_STREAM_UNAVAILABLE
    await client.aclose()


async def test_pool_put_closes_the_replaced_client() -> None:
    fake = FakeOmpweb()
    pool = ClientPool(fake.factory())
    first = pool.build(make_machine(), [])
    second = pool.build(make_machine(), [])
    await pool.put("u1", "box", first)
    await pool.put("u1", "box", second)
    assert first.http.is_closed is True
    assert pool.get("u1", "box") is second
    await pool.close()


def test_validate_host_port_alias_reject_bad_input() -> None:
    assert validate_host("192.168.1.10") == "192.168.1.10"
    assert validate_port(8080) == 8080
    assert validate_alias("box-1") == "box-1"
    for bad_port in (0, 65536):
        with pytest.raises(OmpError):
            validate_port(bad_port)
    with pytest.raises(OmpError):
        validate_alias("has space")


def test_base_url_brackets_ipv6() -> None:
    assert base_url("127.0.0.1", 8080) == "http://127.0.0.1:8080"
    assert base_url("::1", 8080) == "http://[::1]:8080"


def test_redact_removes_raw_and_json_escaped_secrets() -> None:
    secret = 'pa"ss\\word'
    escaped = secret.replace("\\", "\\\\").replace('"', '\\"')
    text = f"raw={secret} json={escaped}"
    cleaned = redact(text, [secret])
    assert secret not in cleaned
    assert escaped not in cleaned
