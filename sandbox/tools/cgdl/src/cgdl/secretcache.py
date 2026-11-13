"""构建脚本 secret 的本地缓存。

只缓存从构建脚本提取出的 secret 及其来源 URL,不缓存逐请求的 token。
文件缺失、损坏或目录不可写时视为无缓存,由调用方重新发现。
缓存目录:环境变量 CGDL_CACHE_DIR,否则 ~/.cache/cgdl。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from cgdl.errors import CgdlError
from cgdl.files import atomic_write

CACHE_FILE = "token_secret.json"


def default_cache_dir() -> Path:
    override = os.environ.get("CGDL_CACHE_DIR")
    return Path(override) if override else Path.home() / ".cache" / "cgdl"


class SecretCache:
    def __init__(self, directory: Path) -> None:
        self._path = directory / CACHE_FILE

    def load(self) -> tuple[str, str] | None:
        """返回 (构建脚本 URL, secret);无有效缓存返回 None。"""
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        url, secret = data.get("build_url"), data.get("secret")
        if isinstance(url, str) and isinstance(secret, str) and secret:
            return url, secret
        return None

    def store(self, build_url: str, secret: str) -> None:
        payload = json.dumps({"build_url": build_url, "secret": secret}).encode("utf-8")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(self._path, payload)
        except (OSError, CgdlError):
            return

    def clear(self) -> None:
        try:
            self._path.unlink()
        except OSError:
            return
