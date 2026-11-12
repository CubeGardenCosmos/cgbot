"""私聊乐谱文件段:识别扩展名、经 NapCat ``get_file`` 或段内 URL 取回字节(有界、失败软处理)。"""

from __future__ import annotations

import asyncio
import base64
import binascii
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path, PurePath

from cgbot.vision.fetch import Fetch, ImageError, default_fetch, fetch_image

SCORE_EXTENSIONS = frozenset({".mid", ".midi", ".musicxml", ".xml", ".mxl"})
GET_FILE_TIMEOUT_SECONDS = 20.0

NapCatCall = Callable[..., Awaitable[dict[str, object]]]


class ScoreFetchError(Exception):
    """取回失败;``code`` 是稳定的短码,不含 URL、路径或文件内容。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def score_extension(data: Mapping[str, object]) -> str | None:
    """文件段的乐谱扩展名(不含点,小写);非乐谱返回 None。MIME 不参与判断。"""
    for key in ("file", "name", "file_name"):
        value = data.get(key)
        if isinstance(value, str) and value:
            suffix = PurePath(value).suffix.casefold()
            if suffix in SCORE_EXTENSIONS:
                return suffix[1:]
    return None


def declared_size(data: Mapping[str, object]) -> int | None:
    value = data.get("file_size")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _as_mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _read_local(path: Path, max_bytes: int) -> bytes:
    if not path.is_file():
        raise FileNotFoundError
    if path.stat().st_size > max_bytes:
        raise ScoreFetchError("too_large")
    return path.read_bytes()


async def fetch_score(
    call: NapCatCall,
    segment: Mapping[str, object],
    *,
    max_bytes: int,
    fetch: Fetch = default_fetch,
) -> bytes:
    """取回一个文件段的字节。

    顺序:先查声明大小;``get_file`` 的 base64 → 本地路径 → URL;最后段内 URL。

    任何失败都抛 :class:`ScoreFetchError`(``too_large`` 或 ``fetch_failed``)。
    """
    size = declared_size(segment)
    if size is not None and size > max_bytes:
        raise ScoreFetchError("too_large")

    params: dict[str, object] = {}
    file_id = segment.get("file_id")
    if isinstance(file_id, str) and file_id:
        params["file_id"] = file_id
    elif isinstance(segment.get("file"), str):
        params["file"] = segment["file"]

    urls: list[str] = []
    if params:
        try:
            response = await call("get_file", params, GET_FILE_TIMEOUT_SECONDS)
        except Exception:  # NotConnected / Timeout / 协议错误:退回段内 URL
            response = {}
        info = _as_mapping(response.get("data"))
        reported = declared_size({"file_size": info.get("file_size")})
        if reported is not None and reported > max_bytes:
            raise ScoreFetchError("too_large")
        encoded = info.get("base64")
        if isinstance(encoded, str) and encoded:
            if len(encoded) > max_bytes * 4 // 3 + 16:
                raise ScoreFetchError("too_large")
            try:
                return base64.b64decode(encoded.removeprefix("base64://"), validate=False)
            except (binascii.Error, ValueError):
                pass
        local = info.get("file")
        if isinstance(local, str) and local.startswith("/"):
            try:
                return await asyncio.to_thread(_read_local, Path(local), max_bytes)
            except ScoreFetchError:
                raise
            except OSError:
                pass  # NapCat 与 bot 不共享文件系统
        remote = info.get("url")
        if isinstance(remote, str):
            urls.append(remote)
    inline = segment.get("url")
    if isinstance(inline, str):
        urls.append(inline)

    for url in urls:
        try:
            data = await fetch_image(url, fetch)
        except ImageError:
            continue
        if len(data) > max_bytes:
            raise ScoreFetchError("too_large")
        return data
    raise ScoreFetchError("fetch_failed")
