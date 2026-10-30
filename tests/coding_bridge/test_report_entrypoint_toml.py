"""Checks that the sandbox entrypoint registers the ``report`` MCP server in Codex config."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import tomllib
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[2]
ENTRYPOINT = REPO / "sandbox" / "bin" / "cgbot-sandbox-entrypoint"

PROXY_ENV = ["HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy"]


def _load_entrypoint() -> ModuleType:
    # The entrypoint has no .py suffix, so spell out the loader.
    loader = importlib.machinery.SourceFileLoader("cgbot_sandbox_entrypoint", str(ENTRYPOINT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_render_config_registers_report_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("HTTP_PROXY", "HTTPS_PROXY"):
        monkeypatch.delenv(name, raising=False)
    entrypoint = _load_entrypoint()
    values = {
        "CGBOT_GATEWAY_URL": "https://gateway.example",
        "CGBOT_MODEL": "gpt-5.5",
        "CGBOT_REASONING_EFFORT": "medium",
    }

    text = entrypoint.render_config(values, tmp_path / "home", tmp_path / "catalog.json")
    config = tomllib.loads(text)

    report = config["mcp_servers"]["report"]
    assert report["command"] == "python3"
    assert report["args"] == [str(entrypoint.REPORT_SCRIPT)]
    assert report["env_vars"] == PROXY_ENV
    assert report["tool_timeout_sec"] == 240
    assert set(config["mcp_servers"]) == {"bridge", "report", "playwright", "lsp"}
