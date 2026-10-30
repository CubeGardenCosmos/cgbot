"""Docker Engine client: typed errors, label filter, and the lifecycle route guard."""

from __future__ import annotations

import json
import re
from collections.abc import Callable

import httpx
import pytest

from cgbot.coding.docker import (
    ApiError,
    Conflict,
    DockerClient,
    ForbiddenRoute,
    NotFound,
)

# Transcribed from the DSH docker-socket-proxy allowlist (docker-proxy/haproxy.cfg). The client
# must only ever send requests that this list permits.
PROXY_ALLOWLIST: tuple[tuple[str, str], ...] = (
    ("GET", r"/(version|_ping|containers/json)"),
    ("GET", r"/containers/[a-zA-Z0-9_.-]+/json"),
    ("GET", r"/exec/[a-zA-Z0-9]+/json"),
    ("GET", r"/networks/[a-zA-Z0-9_.-]+"),
    ("POST", r"/containers/create"),
    ("POST", r"/containers/[a-zA-Z0-9_.-]+/(start|stop|exec)"),
    ("POST", r"/exec/[a-zA-Z0-9]+/start"),
    ("DELETE", r"/containers/[a-zA-Z0-9_.-]+"),
)


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> DockerClient:
    return DockerClient(
        httpx.AsyncClient(
            base_url="http://docker-proxy.test:2375",
            transport=httpx.MockTransport(handler),
            trust_env=False,
        )
    )


def _json(status: int, payload: object) -> httpx.Response:
    return httpx.Response(status, json=payload)


async def test_list_containers_filters_by_label_and_includes_stopped():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _json(200, [{"Id": "abc", "Names": ["/cgbot-sbx-1"], "State": "exited"}])

    items = await _client(handler).list_containers(label="cgbot.managed=true")
    assert items == [{"Id": "abc", "Names": ["/cgbot-sbx-1"], "State": "exited"}]
    params = seen[0].url.params
    assert params["all"] == "true"
    assert json.loads(params["filters"]) == {"label": ["cgbot.managed=true"]}


async def test_missing_container_maps_to_not_found_with_docker_message():
    client = _client(lambda request: _json(404, {"message": "No such container: cgbot-sbx-9"}))
    with pytest.raises(NotFound) as info:
        await client.inspect_container("cgbot-sbx-9")
    assert info.value.status == 404
    assert info.value.message == "No such container: cgbot-sbx-9"


async def test_name_conflict_maps_to_conflict():
    client = _client(lambda request: _json(409, {"message": "name in use"}))
    with pytest.raises(Conflict):
        await client.create_container("cgbot-sbx-1", {"Image": "x"})


async def test_other_error_status_maps_to_api_error_with_plain_text_body():
    client = _client(lambda request: httpx.Response(500, text="plain failure text"))
    with pytest.raises(ApiError) as info:
        await client.remove_container("cgbot-sbx-1", force=True)
    assert info.value.status == 500
    assert info.value.message == "plain failure text"


async def test_start_and_stop_treat_already_in_state_as_success():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        return httpx.Response(304)

    client = _client(handler)
    await client.start_container("cgbot-sbx-1")
    await client.stop_container("cgbot-sbx-1")
    assert calls == [
        "POST /containers/cgbot-sbx-1/start",
        "POST /containers/cgbot-sbx-1/stop",
    ]


async def test_remove_sends_force_flag_and_stop_sends_timeout():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    client = _client(handler)
    await client.stop_container("cgbot-sbx-1", timeout_seconds=7)
    await client.remove_container("cgbot-sbx-1", force=True)
    assert seen[0].url.params["t"] == "7"
    assert seen[1].url.params["force"] == "true"


async def test_guard_refuses_routes_outside_sandbox_lifecycle_without_sending():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    client = _client(handler)
    for method, path in (
        ("POST", "/containers/cgbot-sbx-1/exec"),
        ("GET", "/images/json"),
        ("POST", "/exec/abc123/start"),
        ("GET", "/containers/cgbot-sbx-1/logs"),
    ):
        with pytest.raises(ForbiddenRoute):
            await client._call(method, path)  # noqa: SLF001 - boundary under test
    assert seen == []


async def test_invalid_container_reference_is_rejected_before_any_request():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    with pytest.raises(ValueError):
        await _client(handler).inspect_container("../containers/json")
    assert seen == []


async def test_client_routes_are_all_permitted_by_proxy_allowlist():
    samples = (
        ("GET", "/containers/json"),
        ("GET", "/containers/cgbot-sbx-1001/json"),
        ("POST", "/containers/create"),
        ("POST", "/containers/cgbot-sbx-1001/start"),
        ("POST", "/containers/cgbot-sbx-1001/stop"),
        ("DELETE", "/containers/cgbot-sbx-1001"),
    )
    for method, path in samples:
        assert any(m == method and re.fullmatch(rx, path) for m, rx in PROXY_ALLOWLIST), (
            f"{method} {path} not permitted by the proxy"
        )
