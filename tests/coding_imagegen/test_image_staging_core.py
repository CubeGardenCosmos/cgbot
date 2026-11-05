from __future__ import annotations

import asyncio
import os
import socket
from pathlib import Path

import pytest

from cgbot.coding.contracts import workspace_dir
from cgbot.coding.staging import ImageStager, StageError
from cgbot.vision import ImageError
from cgbot.vision import fetch as vfetch

PNG = b"\x89PNG\r\n\x1a\nimage"
NOW = 1793318400.0
URL = "https://images.example/picture?token=secret"


class Fetch:
    def __init__(self, data: bytes = PNG) -> None:
        self.data = data
        self.calls: list[str] = []

    async def __call__(self, url: str) -> bytes:
        self.calls.append(url)
        return self.data


def host_path(root: Path, result: str, user_id: str = "123") -> Path:
    return workspace_dir(root, user_id) / result.removeprefix("/workspace/")


@pytest.mark.parametrize(
    ("data", "extension"),
    [
        (PNG, "png"),
        (b"\xff\xd8\xffimage", "jpg"),
        (b"RIFF\x04\x00\x00\x00WEBPimage", "webp"),
        (b"GIF87aimage", "gif"),
        (b"GIF89aimage", "gif"),
    ],
)
async def test_sniffs_supported_magic(tmp_path: Path, data: bytes, extension: str) -> None:
    fetch = Fetch(data)
    stager = ImageStager(tmp_path, fetch=fetch, clock=lambda: NOW)
    result = await stager.stage("123", URL)
    assert result.startswith("/workspace/inbox/")
    assert result.endswith(f"-1.{extension}")
    assert host_path(tmp_path, result).read_bytes() == data
    assert fetch.calls == [URL]


@pytest.mark.parametrize("data", [b"", b"not an image", b"RIFFbad", b"GIF88a", b"<svg/>"])
async def test_rejects_unsupported_magic(tmp_path: Path, data: bytes) -> None:
    with pytest.raises(StageError) as caught:
        await ImageStager(tmp_path, fetch=Fetch(data)).stage("123", URL)
    assert caught.value.code == "unsupported_image"
    assert not workspace_dir(tmp_path, "123").exists()


async def test_size_cap_includes_injected_fetch(tmp_path: Path) -> None:
    data = PNG + b"x" * (vfetch.MAX_IMAGE_BYTES - len(PNG))
    result = await ImageStager(tmp_path, fetch=Fetch(data)).stage("123", URL)
    assert host_path(tmp_path, result).stat().st_size == vfetch.MAX_IMAGE_BYTES
    with pytest.raises(StageError) as caught:
        await ImageStager(tmp_path, fetch=Fetch(data + b"x")).stage("123", URL)
    assert caught.value.code == "too_large"


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/a",
        "http://10.0.0.1/a",
        "http://172.16.0.1/a",
        "http://192.168.1.2/a",
        "http://169.254.169.254/a",
        "http://0.0.0.0/a",
        "http://100.64.0.1/a",
        "http://192.0.2.1/a",
        "http://224.0.0.1/a",
        "http://[::1]/a",
        "http://[::]/a",
        "http://[fe80::1]/a",
        "http://[fc00::1]/a",
        "http://[::ffff:127.0.0.1]/a",
        "http://[::ffff:192.168.1.1]/a",
        "http://127.1/a",
        "http://2130706433/a",
        "http://0x7f000001/a",
    ],
)
async def test_blocks_literal_ssrf_before_fetch(tmp_path: Path, url: str) -> None:
    fetch = Fetch()
    with pytest.raises(StageError) as caught:
        await ImageStager(tmp_path, fetch=fetch).stage("123", url)
    assert caught.value.code == "blocked_address"
    assert fetch.calls == []


@pytest.mark.parametrize(
    "url",
    [
        "",
        "file:///tmp/image",
        "ftp://images.example/a",
        "https:///missing",
        "https://user:secret@images.example/a",
        "https://user@images.example/a",
        "https://images.example:bad/a",
        "https://images.example:70000/a",
        "https://images.example:0/a",
        "https://images.example/\nsecret",
        "https://images.example/\tsecret",
        " https://images.example/a",
        "https://images.example/a\x7f",
        "https://images.example\\@127.0.0.1/a",
        "http://[fe80::1%25eth0]/a",
        "https://images.example/a\x85",
        "https://images.example/a\x9f",
    ],
)
async def test_bad_urls_never_fetch(tmp_path: Path, url: str) -> None:
    fetch = Fetch()
    with pytest.raises(StageError) as caught:
        await ImageStager(tmp_path, fetch=fetch).stage("123", url)
    assert caught.value.code == "invalid_url"
    assert fetch.calls == []


@pytest.mark.parametrize("user_id", ["", ".", "..", "../other", "/tmp", "a/b", "a\\b", "a" * 65])
async def test_user_path_rejection(tmp_path: Path, user_id: str) -> None:
    fetch = Fetch()
    with pytest.raises(StageError) as caught:
        await ImageStager(tmp_path, fetch=fetch).stage(user_id, URL)
    assert caught.value.code == "invalid_user_id"
    assert fetch.calls == []


async def test_fetch_failure_has_stable_secret_free_error(tmp_path: Path) -> None:
    async def fail(url: str) -> bytes:
        raise RuntimeError(f"failed fetching {url}")

    with pytest.raises(StageError) as caught:
        await ImageStager(tmp_path, fetch=fail).stage("123", URL)
    assert caught.value.code == "download_failed"
    assert "secret" not in str(caught.value)
    assert caught.value.__suppress_context__


async def test_names_are_generated_exclusively_and_do_not_reuse_remote_path(tmp_path: Path) -> None:
    stager = ImageStager(tmp_path, fetch=Fetch(), clock=lambda: NOW)
    first = await stager.stage("123", "https://images.example/../../outside.exe?name=/secret")
    second = await stager.stage("123", "https://images.example/%2e%2e%2fsecret.jpg")
    assert first.endswith("-1.png")
    assert second.endswith("-2.png")
    assert host_path(tmp_path, first).read_bytes() == PNG
    assert host_path(tmp_path, second).read_bytes() == PNG
    other = await stager.stage("456", URL)
    assert other == first
    assert host_path(tmp_path, other, "456").read_bytes() == PNG


async def test_retains_fifty_regular_staged_files_oldest_first(tmp_path: Path) -> None:
    inbox = workspace_dir(tmp_path, "123") / "inbox"
    inbox.mkdir(parents=True)
    for number in range(1, 52):
        path = inbox / f"20200101-000000-{number}.png"
        path.write_bytes(PNG)
        os.utime(path, ns=(number, number))
    unrelated = inbox / "notes.txt"
    unrelated.write_text("keep")
    directory = inbox / "20200101-000000-80.png"
    directory.mkdir()
    target = tmp_path / "outside"
    target.write_bytes(b"private")
    link = inbox / "20200101-000000-81.png"
    link.symlink_to(target)
    result = await ImageStager(tmp_path, fetch=Fetch()).stage("123", URL)
    assert not (inbox / "20200101-000000-1.png").exists()
    assert not (inbox / "20200101-000000-2.png").exists()
    assert (inbox / "20200101-000000-3.png").exists()
    regular = [
        p for p in inbox.iterdir() if not p.is_symlink() and p.is_file() and p.suffix == ".png"
    ]
    assert len(regular) == 50
    assert host_path(tmp_path, result).exists()
    assert unrelated.read_text() == "keep"
    assert directory.is_dir()
    assert link.is_symlink()
    assert target.read_bytes() == b"private"


@pytest.mark.parametrize("component", ["users", "123", "inbox"])
async def test_rejects_directory_symlinks(tmp_path: Path, component: str) -> None:
    target = tmp_path / "outside"
    target.mkdir()
    parts = {"users": tmp_path / "users", "123": workspace_dir(tmp_path, "123")}
    link = parts.get(component, workspace_dir(tmp_path, "123") / "inbox")
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(StageError) as caught:
        await ImageStager(tmp_path, fetch=Fetch()).stage("123", URL)
    assert caught.value.code == "unsafe_path"
    assert list(target.iterdir()) == []


async def test_rejects_inbox_regular_file(tmp_path: Path) -> None:
    inbox = workspace_dir(tmp_path, "123") / "inbox"
    inbox.parent.mkdir(parents=True)
    inbox.write_text("keep")
    with pytest.raises(StageError) as caught:
        await ImageStager(tmp_path, fetch=Fetch()).stage("123", URL)
    assert caught.value.code == "unsafe_path"
    assert inbox.read_text() == "keep"


async def test_collision_with_symlink_does_not_follow_or_overwrite(tmp_path: Path) -> None:
    stager = ImageStager(tmp_path, fetch=Fetch(), clock=lambda: NOW)
    first = await stager.stage("123", URL)
    path = host_path(tmp_path, first)
    path.unlink()
    target = tmp_path / "outside"
    target.write_bytes(b"private")
    path.symlink_to(target)
    second = await stager.stage("123", URL)
    assert second.endswith("-2.png")
    assert path.is_symlink()
    assert target.read_bytes() == b"private"


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "::ffff:127.0.0.1"])
async def test_resolver_rejects_private_dns_without_network(
    monkeypatch: pytest.MonkeyPatch, address: str
) -> None:
    class Resolver:
        async def resolve(self, host: str, port: int, family: int):
            return [
                {
                    "hostname": host,
                    "host": address,
                    "port": port,
                    "family": family,
                    "proto": 0,
                    "flags": 0,
                }
            ]

        async def close(self) -> None:
            pass

    monkeypatch.setattr(vfetch, "ThreadedResolver", Resolver)
    resolver = vfetch._PublicResolver()
    with pytest.raises(ImageError) as caught:
        await resolver.resolve("images.example", 443, socket.AF_INET)
    assert caught.value.code == "blocked_address"
    await resolver.close()


async def test_resolver_rejects_mixed_public_private_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Resolver:
        async def resolve(self, host: str, port: int, family: int):
            return [
                {
                    "hostname": host,
                    "host": address,
                    "port": port,
                    "family": family,
                    "proto": 0,
                    "flags": 0,
                }
                for address in ["8.8.8.8", "127.0.0.1"]
            ]

        async def close(self) -> None:
            pass

    monkeypatch.setattr(vfetch, "ThreadedResolver", Resolver)
    resolver = vfetch._PublicResolver()
    with pytest.raises(ImageError) as caught:
        await resolver.resolve("images.example", 443, socket.AF_INET)
    assert caught.value.code == "blocked_address"
    await resolver.close()


async def test_injected_fetch_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def slow(url: str) -> bytes:
        await asyncio.sleep(60)
        return PNG

    monkeypatch.setattr(vfetch, "FETCH_TIMEOUT_SECONDS", 0.001)
    with pytest.raises(StageError) as caught:
        await ImageStager(tmp_path, fetch=slow).stage("123", URL)
    assert caught.value.code == "download_failed"


@pytest.mark.parametrize(
    ("status", "length", "chunks", "error"),
    [
        (301, 0, [], "download_failed"),
        (302, 0, [], "download_failed"),
        (307, 0, [], "download_failed"),
        (308, 0, [], "download_failed"),
        (200, vfetch.MAX_IMAGE_BYTES + 1, [], "too_large"),
        (200, None, [b"x" * vfetch.MAX_IMAGE_BYTES, b"x"], "too_large"),
        (200, None, [PNG], None),
    ],
)
async def test_default_fetch_stream_limits_and_redirect_rejection_without_network(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    length: int | None,
    chunks: list[bytes],
    error: str | None,
) -> None:
    class Content:
        async def iter_chunked(self, size: int):
            for chunk in chunks:
                yield chunk

    class Response:
        content_length = length
        content = Content()

        async def __aenter__(self):
            self.status = status
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

    class Session:
        def __init__(self, **kwargs: object) -> None:
            assert kwargs["trust_env"] is False
            assert isinstance(kwargs["timeout"], vfetch.aiohttp.ClientTimeout)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        def get(self, url: str, *, allow_redirects: bool):
            assert url == URL
            assert allow_redirects is False
            return Response()

    class Resolver:
        async def close(self) -> None:
            pass

    monkeypatch.setattr(vfetch, "_PublicResolver", Resolver)
    monkeypatch.setattr(vfetch.aiohttp, "TCPConnector", lambda **kwargs: object())
    monkeypatch.setattr(vfetch.aiohttp, "ClientSession", Session)
    if error is None:
        assert await vfetch.default_fetch(URL) == PNG
    else:
        with pytest.raises(ImageError) as caught:
            await vfetch.default_fetch(URL)
        assert caught.value.code == error


async def test_new_files_remain_sandbox_readable_under_restrictive_umask(tmp_path: Path) -> None:
    previous = os.umask(0o077)
    try:
        result = await ImageStager(tmp_path, fetch=Fetch()).stage("123", URL)
    finally:
        os.umask(previous)
    path = host_path(tmp_path, result)
    assert path.stat().st_mode & 0o777 == 0o644
    assert path.parent.stat().st_mode & 0o777 == 0o755
