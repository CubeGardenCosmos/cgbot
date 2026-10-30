"""Entry point: ``python -m cgbot --config <toml> [--secrets <toml>]``."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from collections.abc import Sequence
from pathlib import Path

from cgbot.app import App
from cgbot.config import Config, ConfigError, load_config
from cgbot.provider import ResponsesProvider


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="cgbot", description="cgbot M1 service")
    parser.add_argument("--config", required=True, type=Path, help="path to config TOML")
    parser.add_argument(
        "--secrets", type=Path, default=None, help="path to secrets TOML (optional)"
    )
    return parser.parse_args(argv)


async def _serve(config: Config) -> None:
    provider = ResponsesProvider(
        base_url=config.provider.base_url,
        api_key=config.provider.api_key.reveal(),
        model=config.provider.model,
        context_window=config.provider.context_window,
    )
    app = App(config, provider)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        await app.start()
        await stop.wait()
    finally:
        await app.stop()
        await provider.aclose()


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        config = load_config(args.config, args.secrets)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    logging.basicConfig(
        level=config.logging.level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(_serve(config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
