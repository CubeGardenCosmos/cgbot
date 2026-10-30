"""Sandbox lifecycle: create/start/ready, spec contract, reuse, recreate, reaper, errors."""

from __future__ import annotations

import asyncio
import json
import re
import stat
from pathlib import Path

import pytest
from sandbox_fakes import (
    NETWORK,
    PROXY,
    TOKEN_SECRET,
    UPSTREAM_KEY,
    ChownRecorder,
    FakeClock,
    FakeDockerd,
    FakeReadyProbe,
    make_config,
    make_provider,
)

from cgbot.coding.auth import HmacSandboxAuth, derive_ws_token
from cgbot.coding.contracts import home_dir, workspace_dir
from cgbot.coding.sandbox import (
    InvalidUserId,
    SandboxBusy,
    SandboxError,
    SandboxNotReady,
    UnmanagedContainer,
)

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
MANAGED = {"cgbot.managed": "true"}


def _env(body: dict) -> dict[str, str]:
    return dict(item.split("=", 1) for item in body["Env"])


async def test_lease_creates_starts_and_yields_endpoint_after_readiness(tmp_path: Path):
    config = make_config(tmp_path)
    dockerd = FakeDockerd()
    probe = FakeReadyProbe(ready_after=1)
    provider = make_provider(config, dockerd, probe=probe)
    try:
        async with provider.lease("1001") as endpoint:
            assert endpoint.user_id == "1001"
            assert endpoint.container == "cgbot-sbx-1001"
            assert endpoint.url == "ws://cgbot-sbx-1001:8765"
            assert endpoint.ws_token == derive_ws_token(config.token_secret, "1001")
            assert dockerd.status("cgbot-sbx-1001") == "running"
        assert len(dockerd.create_bodies) == 1
        assert dockerd.starts == ["cgbot-sbx-1001"]
        assert probe.urls == ["http://cgbot-sbx-1001:8765/readyz"] * 2
    finally:
        await provider.close()


async def test_container_spec_pins_binds_limits_and_single_network(tmp_path: Path):
    config = make_config(tmp_path)
    dockerd = FakeDockerd()
    provider = make_provider(config, dockerd)
    try:
        async with provider.lease("1001"):
            pass
    finally:
        await provider.close()
    body = dockerd.create_bodies[0]
    host = body["HostConfig"]
    data = config.docker.host_data_dir
    assert host["Binds"] == [
        f"{data / 'users' / '1001'}:/workspace:rw",
        f"{data / 'homes' / '1001'}:/home/agent:rw",
    ]
    assert not any("docker.sock" in bind for bind in host["Binds"])
    assert set(host["Tmpfs"]) == {"/tmp"}
    assert host["CapDrop"] == ["ALL"]
    assert host["SecurityOpt"] == ["no-new-privileges:true", "label=disable"]
    assert host["PidsLimit"] == 512
    assert host["Memory"] == 2 * 1024**3
    assert host["NanoCpus"] == 2_000_000_000
    assert host["CpuShares"] == 2
    assert host["Init"] is True
    assert host["RestartPolicy"] == {"Name": "no"}
    assert host["NetworkMode"] == NETWORK
    assert set(body["NetworkingConfig"]["EndpointsConfig"]) == {NETWORK}
    assert body["Image"] == "cgbot-sandbox:test"
    assert body["User"] == "1000:1000"
    assert body["WorkingDir"] == "/workspace"
    assert body["Labels"]["cgbot.managed"] == "true"
    assert body["Labels"]["cgbot.user"] == "1001"


async def test_env_carries_contract_variables_and_no_upstream_secret(tmp_path: Path):
    config = make_config(tmp_path)
    dockerd = FakeDockerd()
    provider = make_provider(config, dockerd)
    try:
        async with provider.lease("1001"):
            pass
    finally:
        await provider.close()
    body = dockerd.create_bodies[0]
    env = _env(body)
    token = HmacSandboxAuth(config.token_secret).token_for("1001")
    assert env["CGBOT_USER_ID"] == "1001"
    assert env["CGBOT_GATEWAY_URL"] == "http://cgbot-bot.test:8080/v1"
    assert env["CGBOT_GATEWAY_TOKEN"] == token
    assert env["CGBOT_BRIDGE_URL"] == "http://cgbot-bot.test:8080/bridge"
    assert env["CGBOT_BRIDGE_TOKEN"] == token
    assert env["CGBOT_WS_TOKEN"] == derive_ws_token(config.token_secret, "1001")
    assert env["CGBOT_APPSERVER_PORT"] == "8765"
    assert env["CGBOT_MODEL"] == "gpt-test"
    assert env["CGBOT_REASONING_EFFORT"] == "medium"
    assert env["HTTP_PROXY"] == PROXY
    assert env["HTTPS_PROXY"] == PROXY
    assert {"cgbot-bot.test", "localhost", "127.0.0.1"} <= set(env["NO_PROXY"].split(","))
    raw = json.dumps(body)
    assert UPSTREAM_KEY not in raw
    assert TOKEN_SECRET not in raw


async def test_each_user_gets_its_own_mounts(tmp_path: Path):
    config = make_config(tmp_path)
    dockerd = FakeDockerd()
    provider = make_provider(config, dockerd)
    try:
        async with provider.lease("1001"):
            pass
        async with provider.lease("1002"):
            pass
    finally:
        await provider.close()
    first, second = (body["HostConfig"]["Binds"] for body in dockerd.create_bodies)
    data = config.docker.host_data_dir
    assert first[0] == f"{workspace_dir(data, '1001')}:/workspace:rw"
    assert second[0] == f"{workspace_dir(data, '1002')}:/workspace:rw"
    assert first[1] == f"{home_dir(data, '1001')}:/home/agent:rw"
    assert second[1] == f"{home_dir(data, '1002')}:/home/agent:rw"


async def test_host_dirs_are_created_owned_by_sandbox_user_and_home_private(tmp_path: Path):
    config = make_config(tmp_path)
    chown = ChownRecorder()
    dockerd = FakeDockerd()
    provider = make_provider(config, dockerd, chown=chown)
    try:
        async with provider.lease("1001"):
            pass
    finally:
        await provider.close()
    data = config.docker.host_data_dir
    workspace, home = workspace_dir(data, "1001"), home_dir(data, "1001")
    assert workspace.is_dir() and home.is_dir()
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    assert chown.calls == [(str(workspace), 1000, 1000), (str(home), 1000, 1000)]


async def test_symlinked_host_dir_is_refused_before_container_creation(tmp_path: Path):
    config = make_config(tmp_path)
    users = config.docker.host_data_dir / "users"
    users.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (users / "1001").symlink_to(elsewhere)
    dockerd = FakeDockerd()
    provider = make_provider(config, dockerd)
    try:
        with pytest.raises(SandboxError):
            async with provider.lease("1001"):
                pass
    finally:
        await provider.close()
    assert dockerd.create_bodies == []


async def test_concurrent_leases_share_one_container(tmp_path: Path):
    config = make_config(tmp_path)
    dockerd = FakeDockerd()
    provider = make_provider(config, dockerd)

    async def use() -> str:
        async with provider.lease("1001") as endpoint:
            await asyncio.sleep(0)
            return endpoint.container

    try:
        names = await asyncio.gather(use(), use(), use())
    finally:
        await provider.close()
    assert names == ["cgbot-sbx-1001"] * 3
    assert len(dockerd.create_bodies) == 1
    assert dockerd.starts == ["cgbot-sbx-1001"]


async def test_stop_is_refused_while_leased_then_stops_without_deleting(tmp_path: Path):
    config = make_config(tmp_path)
    dockerd = FakeDockerd()
    provider = make_provider(config, dockerd)
    try:
        async with provider.lease("1001"):
            with pytest.raises(SandboxBusy):
                await provider.stop("1001")
            assert dockerd.status("cgbot-sbx-1001") == "running"
        await provider.stop("1001")
        assert dockerd.status("cgbot-sbx-1001") == "exited"
        assert dockerd.removes == []
    finally:
        await provider.close()


async def test_config_change_recreates_container_but_keeps_host_mounts(tmp_path: Path):
    dockerd = FakeDockerd()
    first = make_provider(make_config(tmp_path), dockerd)
    try:
        async with first.lease("1001"):
            pass
    finally:
        await first.close()
    second = make_provider(make_config(tmp_path, model="gpt-other"), dockerd)
    try:
        async with second.lease("1001"):
            pass
    finally:
        await second.close()
    assert dockerd.removes == ["cgbot-sbx-1001"]
    assert len(dockerd.create_bodies) == 2
    old, new = dockerd.create_bodies
    assert old["HostConfig"]["Binds"] == new["HostConfig"]["Binds"]
    assert "CGBOT_MODEL=gpt-other" in new["Env"]


async def test_unchanged_config_reuses_running_container_after_restart(tmp_path: Path):
    dockerd = FakeDockerd()
    first = make_provider(make_config(tmp_path), dockerd)
    try:
        async with first.lease("1001"):
            pass
    finally:
        await first.close()
    second = make_provider(make_config(tmp_path), dockerd)
    try:
        await second.reconcile()
        async with second.lease("1001") as endpoint:
            assert endpoint.container == "cgbot-sbx-1001"
    finally:
        await second.close()
    assert len(dockerd.create_bodies) == 1
    assert dockerd.removes == []
    assert dockerd.starts == ["cgbot-sbx-1001"]


async def test_stopped_container_is_restarted_not_recreated(tmp_path: Path):
    config = make_config(tmp_path)
    dockerd = FakeDockerd()
    provider = make_provider(config, dockerd)
    try:
        async with provider.lease("1001"):
            pass
        await provider.stop("1001")
        async with provider.lease("1001"):
            pass
    finally:
        await provider.close()
    assert len(dockerd.create_bodies) == 1
    assert dockerd.starts == ["cgbot-sbx-1001", "cgbot-sbx-1001"]


async def test_reconcile_adopts_only_running_managed_containers_with_expected_name(tmp_path: Path):
    config = make_config(tmp_path, idle_seconds=600)
    dockerd = FakeDockerd()
    dockerd.seed("cgbot-sbx-42", labels={**MANAGED, "cgbot.user": "42"})
    dockerd.seed("cgbot-sbx-43", labels={**MANAGED, "cgbot.user": "43"}, status="exited")
    dockerd.seed("renamed", labels={**MANAGED, "cgbot.user": "44"})
    dockerd.seed("unrelated", labels={"owner": "someone"})
    clock = FakeClock()
    provider = make_provider(config, dockerd, clock=clock)
    try:
        await provider.reconcile()
        clock.now += 600
        await provider.reap_once()
    finally:
        await provider.close()
    assert dockerd.stops == ["cgbot-sbx-42"]
    assert dockerd.status("cgbot-sbx-42") == "exited"
    assert dockerd.status("cgbot-sbx-43") == "exited"
    assert dockerd.status("renamed") == "running"
    assert dockerd.status("unrelated") == "running"


async def test_idle_reaper_stops_after_window_keeps_container_and_home(tmp_path: Path):
    config = make_config(tmp_path, idle_seconds=600)
    dockerd = FakeDockerd()
    clock = FakeClock()
    provider = make_provider(config, dockerd, clock=clock)
    try:
        async with provider.lease("1001"):
            pass
        clock.now += 599
        await provider.reap_once()
        assert dockerd.status("cgbot-sbx-1001") == "running"
        clock.now += 1
        await provider.reap_once()
        assert dockerd.status("cgbot-sbx-1001") == "exited"
        assert dockerd.removes == []
        async with provider.lease("1001"):
            pass
    finally:
        await provider.close()
    assert len(dockerd.create_bodies) == 1
    assert dockerd.status("cgbot-sbx-1001") == "running"


async def test_reaper_never_stops_a_leased_sandbox(tmp_path: Path):
    config = make_config(tmp_path, idle_seconds=600)
    dockerd = FakeDockerd()
    clock = FakeClock()
    provider = make_provider(config, dockerd, clock=clock)
    try:
        async with provider.lease("1001"):
            clock.now += 10_000
            await provider.reap_once()
            assert dockerd.status("cgbot-sbx-1001") == "running"
        assert dockerd.stops == []
    finally:
        await provider.close()


async def test_readiness_timeout_stops_container_and_raises(tmp_path: Path):
    config = make_config(tmp_path)
    dockerd = FakeDockerd()
    probe = FakeReadyProbe(never=frozenset({"cgbot-sbx-1001"}))
    provider = make_provider(config, dockerd, probe=probe, ready_timeout=3.0)
    try:
        with pytest.raises(SandboxNotReady):
            async with provider.lease("1001"):
                pass
        assert dockerd.status("cgbot-sbx-1001") == "exited"
        assert dockerd.removes == []
        assert len(probe.urls) == 4  # probes at t=0,1,2,3 with a 3 s budget and 1 s interval
    finally:
        await provider.close()


async def test_unmanaged_container_with_expected_name_is_refused(tmp_path: Path):
    config = make_config(tmp_path)
    dockerd = FakeDockerd()
    dockerd.seed("cgbot-sbx-1001", labels={"owner": "someone-else"})
    provider = make_provider(config, dockerd)
    try:
        with pytest.raises(UnmanagedContainer):
            async with provider.lease("1001"):
                pass
    finally:
        await provider.close()
    assert dockerd.starts == []
    assert dockerd.stops == []
    assert dockerd.removes == []


@pytest.mark.parametrize(
    "bad",
    ["", " 1001", "../1001", "1001;rm", "a" * 65, "\uff11\uff10\uff10\uff11", "a/b"],
)
async def test_invalid_user_id_is_rejected_before_any_docker_call(tmp_path: Path, bad: str):
    config = make_config(tmp_path)
    dockerd = FakeDockerd()
    provider = make_provider(config, dockerd)
    try:
        with pytest.raises(InvalidUserId):
            provider.lease(bad)
        with pytest.raises(InvalidUserId):
            await provider.stop(bad)
    finally:
        await provider.close()
    assert dockerd.requests == []


async def test_every_request_to_docker_is_permitted_by_proxy_allowlist(tmp_path: Path):
    config = make_config(tmp_path, idle_seconds=600)
    dockerd = FakeDockerd()
    clock = FakeClock()
    provider = make_provider(config, dockerd, clock=clock)
    try:
        async with provider.lease("1001"):
            pass
        await provider.stop("1001")
        async with provider.lease("1001"):
            pass
        clock.now += 600
        await provider.reap_once()
        await provider.reconcile()
    finally:
        await provider.close()
    assert dockerd.requests
    for method, path in dockerd.requests:
        assert any(m == method and re.fullmatch(rx, path) for m, rx in PROXY_ALLOWLIST), (
            f"{method} {path} is not permitted by the proxy"
        )


async def test_start_reconciles_then_close_cancels_reaper(tmp_path: Path):
    config = make_config(tmp_path)
    dockerd = FakeDockerd()
    dockerd.seed("cgbot-sbx-1001", labels={**MANAGED, "cgbot.user": "1001"})
    provider = make_provider(config, dockerd)
    await provider.start()
    try:
        assert ("GET", "/containers/json") in dockerd.requests
        assert "coding-sandbox-reaper" in {t.get_name() for t in asyncio.all_tasks()}
    finally:
        await provider.close()
    assert "coding-sandbox-reaper" not in {t.get_name() for t in asyncio.all_tasks()}
    assert dockerd.create_bodies == []
    assert dockerd.stops == []
    assert dockerd.removes == []
