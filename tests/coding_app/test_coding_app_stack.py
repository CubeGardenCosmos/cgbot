"""CodingStack: lifecycle ordering, failure rollback, isolated stop, wiring of tools and routes."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from coding_app_fakes import FakePart, NullNotifier, coding_config

import cgbot.coding.stack as stack_module
from cgbot.coding.auth import HmacSandboxAuth
from cgbot.coding.governor import ConcurrencyGovernor
from cgbot.coding.stack import CodingStack
from cgbot.config.settings import SearchConfig, Secret
from cgbot.search import SearchService
from cgbot.search.gemini import GeminiSearchClient

TOOL_NAMES = {
    "codex_submit",
    "codex_list",
    "codex_peek",
    "codex_read",
    "codex_steer",
    "codex_stop",
    "codex_compact",
    "codex_rename",
    "codex_archive",
    "codex_delete",
    "codex_fork",
    "codex_btw",
}
STOP_ORDER = ["web.stop", "bridge.close", "manager.close", "sandbox.close", "imagegen.close"]


def _install(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
    fail_on: set[str] | None = None,
) -> dict[str, FakePart]:
    """Replace every lifecycle part in the stack module with a recording fake."""
    failing = fail_on or set()
    parts: dict[str, FakePart] = {}

    def factory(name: str, routes: tuple[object, ...] = ()):
        def build(*_args: object, **kwargs: object) -> FakePart:
            part = FakePart(name, events, failing, routes=routes)
            part.kwargs = dict(kwargs)
            parts[name] = part
            return part

        return build

    monkeypatch.setattr(stack_module, "QueueStore", lambda *_a, **_k: object())
    monkeypatch.setattr(stack_module, "DockerSandboxProvider", factory("sandbox"))
    monkeypatch.setattr(stack_module, "CodingManager", factory("manager"))
    monkeypatch.setattr(stack_module, "bridge_routes", factory("bridge", routes=("bridge-route",)))
    monkeypatch.setattr(stack_module, "ImageGenService", factory("imagegen"))
    monkeypatch.setattr(stack_module, "CodingWeb", factory("web"))
    monkeypatch.setattr(stack_module, "imagegen_routes", lambda _service, _auth: ["imagegen-route"])
    monkeypatch.setattr(
        stack_module, "search_routes", lambda _service, _gemini, _auth: ["search-route"]
    )
    return parts


def _stack(tmp_path: Path) -> CodingStack:
    return CodingStack(
        coding_config(tmp_path),
        upstream_base_url="https://upstream.example/v1",
        upstream_api_key=Secret("upstream-key"),
        notifier=NullNotifier(),
        queue_path=tmp_path / "queue.sqlite",
    )


async def test_start_runs_parts_in_order_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    _install(monkeypatch, events)
    stack = _stack(tmp_path)

    await stack.start()
    await stack.start()

    assert events == ["sandbox.start", "manager.start", "bridge.start", "web.start"]


async def test_stop_after_start_runs_in_reverse_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    _install(monkeypatch, events)
    stack = _stack(tmp_path)

    await stack.start()
    events.clear()
    await stack.stop()

    assert events == STOP_ORDER


async def test_stop_without_start_still_closes_every_part(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    _install(monkeypatch, events)
    await _stack(tmp_path).stop()
    assert events == STOP_ORDER


async def test_failed_start_rolls_back_started_parts_and_reraises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    _install(monkeypatch, events, fail_on={"bridge.start"})
    stack = _stack(tmp_path)

    with pytest.raises(RuntimeError, match="fake failure"):
        await stack.start()

    assert events == ["sandbox.start", "manager.start", "bridge.start", *STOP_ORDER]
    assert "web.start" not in events


async def test_stop_isolates_a_failing_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    events: list[str] = []
    _install(monkeypatch, events, fail_on={"manager.close"})
    stack = _stack(tmp_path)

    with caplog.at_level(logging.WARNING):
        await stack.stop()

    assert events == STOP_ORDER
    assert "coding manager stop failed: error=RuntimeError" in caplog.text


async def test_web_gets_governor_hook_and_all_route_sets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parts = _install(monkeypatch, [])
    _stack(tmp_path)

    web_kwargs = parts["web"].kwargs
    hook = web_kwargs["status_hook"]
    assert isinstance(getattr(hook, "__self__", None), ConcurrencyGovernor)
    assert web_kwargs["extra_routes"] == ["bridge-route", "imagegen-route", "search-route"]


@pytest.mark.parametrize("enabled", [False, True])
async def test_search_routes_receive_shared_services_and_sandbox_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    parts = _install(monkeypatch, [])
    service = SearchService(SearchConfig()) if enabled else None
    gemini = (
        GeminiSearchClient("https://relay.example/v1beta", "test-key", "gemini-test")
        if enabled
        else None
    )
    received: list[tuple[SearchService | None, GeminiSearchClient | None, HmacSandboxAuth]] = []

    def routes(
        search: SearchService | None,
        client: GeminiSearchClient | None,
        auth: HmacSandboxAuth,
    ) -> list[str]:
        received.append((search, client, auth))
        return ["web-search-route", "gemini-search-route"]

    monkeypatch.setattr(stack_module, "search_routes", routes)
    CodingStack(
        coding_config(tmp_path),
        upstream_base_url="https://upstream.example/v1",
        upstream_api_key=Secret("upstream-key"),
        notifier=NullNotifier(),
        queue_path=tmp_path / "queue.sqlite",
        search_service=service,
        gemini_client=gemini,
    )

    assert len(received) == 1
    assert received[0][:2] == (service, gemini)
    assert isinstance(received[0][2], HmacSandboxAuth)
    assert parts["web"].kwargs["extra_routes"] == [
        "bridge-route",
        "imagegen-route",
        "web-search-route",
        "gemini-search-route",
    ]
    if service is not None:
        await service.close()


async def test_tools_are_the_codex_family(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, [])
    stack = _stack(tmp_path)
    assert {spec.name for spec in stack.tools} == TOOL_NAMES


async def test_real_parts_construct_and_stop_cleanly_without_start(tmp_path: Path) -> None:
    stack = _stack(tmp_path)
    assert {spec.name for spec in stack.tools} == TOOL_NAMES
    await stack.stop()
