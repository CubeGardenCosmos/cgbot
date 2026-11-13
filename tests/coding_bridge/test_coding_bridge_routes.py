"""Bridge HTTP routes: upload, chunk, complete, report and download (offline test client)."""

from __future__ import annotations

import hashlib
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from bridge_fakes import PUBLIC_BASE, FakeAuth, FakeClock, FakeNotifier, MiB, make_config

from cgbot.coding.bridge import BridgeHandle, bridge_routes, sniff_mime

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
TEXT = b"hello bridge\n"
BLOB = bytes([0x00, 0x01, 0x02, 0xFE, 0xFF, 0x10])
AUDIO = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 16


@dataclass
class Bridge:
    client: TestClient
    handle: BridgeHandle
    notifier: FakeNotifier
    clock: FakeClock
    temp: Path


@asynccontextmanager
async def open_bridge(
    tmp_path: Path,
    *,
    notifier: FakeNotifier | None = None,
    clock: FakeClock | None = None,
    **config_kw: Any,
) -> AsyncIterator[Bridge]:
    temp = tmp_path / "temp"
    notifier = notifier or FakeNotifier()
    clock = clock or FakeClock()
    config = make_config(temp, **config_kw)
    auth = FakeAuth({"tok-alice": "alice", "tok-bob": "bob"})
    handle = bridge_routes(config, auth, notifier, clock=clock)
    app = web.Application()
    app.add_routes(handle.routes)
    await handle.start()
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield Bridge(client, handle, notifier, clock, temp)
    finally:
        await client.close()
        await handle.close()


def auth(user: str = "alice") -> dict[str, str]:
    return {"Authorization": f"Bearer tok-{user}"}


async def open_upload(b: Bridge, name: str, size: int, user: str = "alice") -> str:
    resp = await b.client.post(
        "/bridge/uploads", json={"name": name, "size": size}, headers=auth(user)
    )
    assert resp.status == 201, await resp.text()
    return str((await resp.json())["upload_id"])


async def put_chunk(
    b: Bridge, upload_id: str, index: int, data: bytes, user: str = "alice"
) -> tuple[int, dict[str, Any]]:
    resp = await b.client.put(
        f"/bridge/uploads/{upload_id}/chunks/{index}", data=data, headers=auth(user)
    )
    return resp.status, await resp.json()


async def complete(
    b: Bridge, upload_id: str, size: int, sha256: str | None = None, user: str = "alice"
) -> tuple[int, dict[str, Any]]:
    body: dict[str, Any] = {"size": size}
    if sha256 is not None:
        body["sha256"] = sha256
    resp = await b.client.post(
        f"/bridge/uploads/{upload_id}/complete", json=body, headers=auth(user)
    )
    return resp.status, await resp.json()


async def report(
    b: Bridge, payload: dict[str, Any], user: str = "alice"
) -> tuple[int, dict[str, Any]]:
    resp = await b.client.post("/bridge/report", json=payload, headers=auth(user))
    return resp.status, await resp.json()


async def upload_file(b: Bridge, name: str, data: bytes) -> tuple[str, dict[str, Any]]:
    uid = await open_upload(b, name, len(data))
    status, _ = await put_chunk(b, uid, 0, data)
    assert status == 200
    status, body = await complete(b, uid, len(data), hashlib.sha256(data).hexdigest())
    assert status == 200, body
    return uid, body


def _path(url: str) -> str:
    return urlparse(url).path


# ---------------------------------------------------------------- happy path


async def test_round_trip_renames_by_sniffed_type_and_downloads(tmp_path: Path) -> None:
    async with open_bridge(tmp_path) as b:
        uid = await open_upload(b, "pic.txt", len(PNG))
        status, body = await put_chunk(b, uid, 0, PNG)
        assert (status, body) == (200, {"index": 0, "received": len(PNG)})
        sha = hashlib.sha256(PNG).hexdigest()
        status, body = await complete(b, uid, len(PNG), sha)
        assert status == 200
        assert body == {
            "upload_id": uid,
            "name": "pic.png",
            "mime": "image/png",
            "size": len(PNG),
            "kind": "image",
        }

        status, body = await report(
            b,
            {
                "message": "done",
                "status": "completed",
                "files": [{"upload_id": uid}],
                "thread_id": "th-1",
            },
        )
        assert (status, body) == (200, {"ok": True})
        (notice,) = b.notifier.notices
        assert notice.kind == "report"
        assert notice.user_id == "alice"
        assert notice.thread_id == "th-1"
        assert notice.status == "completed"
        assert notice.text == "done"
        (stored,) = notice.files
        assert stored.name == "pic.png"
        assert stored.mime == "image/png"
        assert stored.kind == "image"
        assert stored.size == len(PNG)
        assert stored.path.read_bytes() == PNG
        assert stored.url.startswith(f"{PUBLIC_BASE}/bridge/files/")
        assert stored.url.endswith("/pic.png")

        resp = await b.client.get(_path(stored.url))
        assert resp.status == 200
        assert await resp.read() == PNG
        assert resp.headers["Content-Type"] == "image/png"
        assert resp.headers["Content-Disposition"] == 'attachment; filename="pic.png"'
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert resp.headers["Cache-Control"] == "no-store"


async def test_text_and_binary_keep_their_sanitized_names(tmp_path: Path) -> None:
    async with open_bridge(tmp_path) as b:
        _, text_body = await upload_file(b, "notes.md", TEXT)
        _, blob_body = await upload_file(b, "dump.exe", BLOB)
    assert text_body["name"] == "notes.md"
    assert text_body["mime"] == "text/plain"
    assert blob_body["name"] == "dump.exe"
    assert blob_body["mime"] == "application/octet-stream"
    assert blob_body["kind"] == "file"


async def test_upload_name_is_reduced_to_a_safe_basename(tmp_path: Path) -> None:
    async with open_bridge(tmp_path) as b:
        _, body = await upload_file(b, "../../etc/evil.bin", BLOB)
        uid_dir = b.temp / "alice"
        stored_paths = list(uid_dir.rglob("*.bin"))
    assert body["name"] == "evil.bin"
    assert len(stored_paths) == 1
    assert stored_paths[0].is_relative_to(b.temp)


# ---------------------------------------------------------------- authentication


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer nope"}, {"Authorization": "Basic tok-alice"}],
    ids=["missing", "unknown-token", "wrong-scheme"],
)
async def test_missing_or_invalid_token_is_401(tmp_path: Path, headers: dict[str, str]) -> None:
    async with open_bridge(tmp_path) as b:
        resp = await b.client.post(
            "/bridge/uploads", json={"name": "a", "size": 1}, headers=headers
        )
        assert resp.status == 401
        assert (await resp.json())["error"] == "unauthorized"
        resp = await b.client.post("/bridge/report", json={"message": "x"}, headers=headers)
        assert resp.status == 401


async def test_upload_belongs_to_its_owner(tmp_path: Path) -> None:
    async with open_bridge(tmp_path) as b:
        uid = await open_upload(b, "a.bin", len(BLOB), user="alice")
        assert (await put_chunk(b, uid, 0, BLOB, user="bob"))[0] == 403
        assert (await complete(b, uid, len(BLOB), user="bob"))[0] == 403
        assert (await put_chunk(b, uid, 0, BLOB))[0] == 200
        assert (await complete(b, uid, len(BLOB)))[0] == 200
        status, body = await report(b, {"message": "x", "files": [{"upload_id": uid}]}, user="bob")
        assert status == 403
        assert body["error"] == "forbidden"
        assert b.notifier.notices == []


async def test_unknown_upload_is_404(tmp_path: Path) -> None:
    async with open_bridge(tmp_path) as b:
        status, body = await put_chunk(b, "0" * 32, 0, BLOB)
        assert status == 404
        assert body["error"] == "unknown_upload"


# ---------------------------------------------------------------- chunk protocol


async def test_chunks_must_be_sequential_with_replace_of_last(tmp_path: Path) -> None:
    async with open_bridge(tmp_path, chunk_bytes=4) as b:
        uid = await open_upload(b, "a.bin", 12)
        assert (await put_chunk(b, uid, 0, b"AAAA"))[0] == 200
        status, body = await put_chunk(b, uid, 2, b"CCCC")
        assert (status, body["error"]) == (409, "out_of_order")
        assert (await put_chunk(b, uid, 0, b"AAAA"))[0] == 200  # replace last sent chunk
        assert (await put_chunk(b, uid, 1, b"BBBB"))[0] == 200
        assert (await put_chunk(b, uid, 2, b"CCCC"))[0] == 200
        status, body = await complete(b, uid, 12)
        assert status == 200
        stored = next(iter(b.temp.rglob("a.bin")))
        assert stored.read_bytes() == b"AAAABBBBCCCC"


async def test_short_chunk_closes_the_upload(tmp_path: Path) -> None:
    async with open_bridge(tmp_path, chunk_bytes=4) as b:
        uid = await open_upload(b, "a.bin", 10)
        assert (await put_chunk(b, uid, 0, b"AAAA"))[0] == 200
        assert (await put_chunk(b, uid, 1, b"BB"))[0] == 200
        status, body = await put_chunk(b, uid, 2, b"CC")
        assert (status, body["error"]) == (409, "upload_closed")


async def test_chunk_size_rules(tmp_path: Path) -> None:
    async with open_bridge(tmp_path, chunk_bytes=4) as b:
        uid = await open_upload(b, "a.bin", 8)
        status, body = await put_chunk(b, uid, 0, b"AAAAA")
        assert (status, body["error"]) == (413, "chunk_too_large")
        status, body = await put_chunk(b, uid, 0, b"")
        assert (status, body["error"]) == (400, "empty_chunk")
        status, body = await put_chunk(b, uid, 0, b"A")  # still usable after rejects
        assert status == 200

        async def chunked() -> AsyncIterator[bytes]:
            yield b"BBBB"

        resp = await b.client.put(f"/bridge/uploads/{uid}/chunks/1", data=chunked(), headers=auth())
        assert resp.status == 411
        assert (await resp.json())["error"] == "length_required"

        resp = await b.client.put(f"/bridge/uploads/{uid}/chunks/abc", data=b"A", headers=auth())
        assert resp.status == 400


async def test_default_two_mib_chunk_boundary(tmp_path: Path) -> None:
    data = PNG + b"\x00" * (2 * MiB - len(PNG))
    async with open_bridge(tmp_path) as b:
        uid = await open_upload(b, "big.bin", 2 * MiB + 1)
        status, body = await put_chunk(b, uid, 0, data + b"\x00")
        assert (status, body["error"]) == (413, "chunk_too_large")
        assert (await put_chunk(b, uid, 0, data))[0] == 200
        assert (await put_chunk(b, uid, 1, b"\x00"))[0] == 200
        status, body = await complete(b, uid, 2 * MiB + 1)
        assert (status, body["mime"]) == (200, "image/png")


async def test_declared_size_and_completion_checks(tmp_path: Path) -> None:
    async with open_bridge(tmp_path, chunk_bytes=4) as b:
        uid = await open_upload(b, "a.bin", 6)
        assert (await put_chunk(b, uid, 0, b"AAAA"))[0] == 200
        status, body = await complete(b, uid, 6)
        assert (status, body["error"]) == (409, "incomplete")
        status, body = await complete(b, uid, 7)
        assert (status, body["error"]) == (400, "size_mismatch")
        status, body = await put_chunk(b, uid, 1, b"BBBB")  # overruns declared size
        assert (status, body["error"]) == (400, "size_mismatch")
        # the failed chunk is rolled back: the client re-sends index 1 with the right length
        assert (await put_chunk(b, uid, 1, b"BB"))[0] == 200
        status, body = await complete(b, uid, 6)
        assert status == 200
        assert body["size"] == 6


async def test_sha256_mismatch_and_malformed_digest(tmp_path: Path) -> None:
    async with open_bridge(tmp_path) as b:
        uid = await open_upload(b, "a.bin", len(TEXT))
        assert (await put_chunk(b, uid, 0, TEXT))[0] == 200
        wrong = hashlib.sha256(b"other").hexdigest()
        status, body = await complete(b, uid, len(TEXT), wrong)
        assert (status, body["error"]) == (400, "sha256_mismatch")
        status, _ = await complete(b, uid, len(TEXT), "not-a-digest")
        assert status == 400
        right = hashlib.sha256(TEXT).hexdigest()
        status, body = await complete(b, uid, len(TEXT), right.upper())
        assert status == 200


async def test_file_cap_streams_and_cleans_up(tmp_path: Path) -> None:
    async with open_bridge(tmp_path, max_file_bytes=10, chunk_bytes=4) as b:
        resp = await b.client.post(
            "/bridge/uploads", json={"name": "a", "size": 11}, headers=auth()
        )
        assert resp.status == 413
        assert (await resp.json())["error"] == "file_too_large"

        uid = await open_upload(b, "a.bin", 8)
        assert (await put_chunk(b, uid, 0, b"AAAA"))[0] == 200
        assert (await put_chunk(b, uid, 1, b"BBBB"))[0] == 200
        status, body = await put_chunk(b, uid, 2, b"CCCC")
        assert (status, body["error"]) == (413, "file_too_large")
        assert not (b.temp / "alice" / uid).exists()
        status, body = await put_chunk(b, uid, 2, b"CC")
        assert (status, body["error"]) == (404, "unknown_upload")


# ---------------------------------------------------------------- report validation


async def test_report_validation_errors(tmp_path: Path) -> None:
    async with open_bridge(tmp_path) as b:
        pending = await open_upload(b, "p.bin", len(BLOB))
        cases: list[tuple[dict[str, Any], int, str]] = [
            ({"message": 123}, 400, "bad_request"),
            ({"message": "x" * 8001}, 400, "message_too_long"),
            ({"message": "x", "status": "done"}, 400, "bad_request"),
            ({"message": "x", "thread_id": ""}, 400, "bad_request"),
            (
                {"message": "x", "files": [{"upload_id": f"id{i}"} for i in range(17)]},
                400,
                "too_many_files",
            ),
            (
                {"message": "x", "files": [{"upload_id": pending}, {"upload_id": pending}]},
                400,
                "duplicate_file",
            ),
            ({"message": "x", "files": [{"upload_id": pending}]}, 409, "file_not_ready"),
        ]
        for payload, status_want, code in cases:
            status, body = await report(b, payload)
            assert (status, body["error"]) == (status_want, code), payload.keys()
        assert b.notifier.notices == []

        resp = await b.client.post("/bridge/report", data=b"x" * (70 * 1024), headers=auth())
        assert resp.status == 413


async def test_report_delivery_failure_is_503_without_leaking_the_body(tmp_path: Path) -> None:
    async with open_bridge(tmp_path, notifier=FakeNotifier(fail=True)) as b:
        resp = await b.client.post("/bridge/report", json={"message": "hi"}, headers=auth())
        text = await resp.text()
        assert resp.status == 503
        assert "notify_failed" in text
        assert "SECRET-BODY-TEXT" not in text


async def test_report_status_defaults_to_running_and_user_is_from_token(tmp_path: Path) -> None:
    async with open_bridge(tmp_path) as b:
        status, _ = await report(b, {"message": "working", "user_id": "bob"}, user="alice")
        assert status == 200
        (notice,) = b.notifier.notices
        assert notice.status == "running"
        assert notice.user_id == "alice"
        assert notice.thread_id is None
        assert notice.files == ()


# ---------------------------------------------------------------- send_as


async def test_send_as_is_carried_per_audio_file(tmp_path: Path) -> None:
    async with open_bridge(tmp_path) as b:
        voice_id, _ = await upload_file(b, "v.mp3", AUDIO)
        file_id, _ = await upload_file(b, "f.mp3", AUDIO)
        plain_id, _ = await upload_file(b, "p.mp3", AUDIO)
        status, _ = await report(
            b,
            {
                "message": "m",
                "files": [
                    {"upload_id": voice_id, "send_as": "voice"},
                    {"upload_id": file_id, "send_as": "file"},
                    {"upload_id": plain_id},
                ],
            },
        )
        assert status == 200
        (notice,) = b.notifier.notices
        assert [f.kind for f in notice.files] == ["audio", "audio", "audio"]
        assert [f.send_as for f in notice.files] == ["voice", "file", None]


async def test_send_as_rejects_unknown_value_and_non_audio_files(tmp_path: Path) -> None:
    async with open_bridge(tmp_path) as b:
        audio_id, _ = await upload_file(b, "a.mp3", AUDIO)
        png_id, _ = await upload_file(b, "p.png", PNG)
        text_id, _ = await upload_file(b, "t.txt", TEXT)
        cases: list[tuple[dict[str, Any], str]] = [
            ({"upload_id": audio_id, "send_as": "record"}, "send_as must be 'voice' or 'file'"),
            ({"upload_id": png_id, "send_as": "voice"}, "send_as is valid only for audio files"),
            ({"upload_id": png_id, "send_as": "file"}, "send_as is valid only for audio files"),
            ({"upload_id": text_id, "send_as": "file"}, "send_as is valid only for audio files"),
        ]
        for entry, message in cases:
            status, body = await report(b, {"message": "x", "files": [entry]})
            assert (status, body["error"], body["message"]) == (400, "bad_request", message)
        assert b.notifier.notices == []


# ---------------------------------------------------------------- download


async def test_download_supports_range_and_rejects_wrong_name_or_token(tmp_path: Path) -> None:
    async with open_bridge(tmp_path) as b:
        _, body = await upload_file(b, "pic.bin", PNG)
        status, _ = await report(b, {"message": "m", "files": [{"upload_id": body["upload_id"]}]})
        assert status == 200
        stored = b.notifier.notices[0].files[0]
        path = _path(stored.url)
        token, name = path.split("/")[-2], path.split("/")[-1]

        resp = await b.client.get(path, headers={"Range": "bytes=0-3"})
        assert resp.status == 206
        assert await resp.read() == PNG[:4]

        resp = await b.client.get(path, headers={"Range": "bytes=999999-1000000"})
        assert resp.status == 416

        assert (await b.client.get(f"/bridge/files/{token}/other.png")).status == 404
        assert (await b.client.get(f"/bridge/files/{'x' * 32}/{name}")).status == 404


# ---------------------------------------------------------------- retention


async def test_partial_upload_expires_and_activity_refreshes_it(tmp_path: Path) -> None:
    clock = FakeClock(1000.0)
    async with open_bridge(tmp_path, clock=clock, file_ttl_seconds=100.0, chunk_bytes=4) as b:
        uid = await open_upload(b, "a.bin", 8)
        clock.now = 1060.0
        assert (await put_chunk(b, uid, 0, b"AAAA"))[0] == 200  # expiry now 1160
        clock.now = 1150.0
        await b.handle.sweep()
        assert (await put_chunk(b, uid, 1, b"BBBB"))[0] == 200  # still alive
        clock.now = 1400.0
        await b.handle.sweep()
        assert not (b.temp / "alice" / uid).exists()
        assert (await put_chunk(b, uid, 2, b"CC"))[0] == 404


async def test_completed_file_expires_and_bytes_are_removed(tmp_path: Path) -> None:
    clock = FakeClock(1000.0)
    async with open_bridge(tmp_path, clock=clock, file_ttl_seconds=100.0) as b:
        uid, _ = await upload_file(b, "pic.png", PNG)
        await report(b, {"message": "m", "files": [{"upload_id": uid}]})
        stored = b.notifier.notices[0].files[0]
        assert stored.path.exists()
        clock.now = 1099.0
        assert (await b.client.get(_path(stored.url))).status == 200
        clock.now = 1200.0
        await b.handle.sweep()
        assert not stored.path.exists()
        assert (await b.client.get(_path(stored.url))).status == 404


async def test_orphan_directories_are_swept_by_mtime(tmp_path: Path) -> None:
    clock = FakeClock(1000.0)
    async with open_bridge(tmp_path, clock=clock, file_ttl_seconds=100.0) as b:
        old = b.temp / "alice" / "orphan-old"
        fresh = b.temp / "alice" / "orphan-fresh"
        old.mkdir(parents=True)
        fresh.mkdir(parents=True)
        os.utime(old, (0.0, 0.0))
        os.utime(fresh, (950.0, 950.0))
        await b.handle.sweep()
        assert not old.exists()
        assert fresh.exists()


# ---------------------------------------------------------------- sniffing


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * 8, ("image/png", "image")),
        (b"\xff\xd8\xff\xe0" + b"\x00" * 8, ("image/jpeg", "image")),
        (b"GIF89a" + b"\x00" * 8, ("image/gif", "image")),
        (b"%PDF-1.7\n", ("application/pdf", "file")),
        (b"ID3\x03\x00\x00\x00\x00\x00\x00", ("audio/mpeg", "audio")),
        (b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 8, ("video/mp4", "video")),
        (b"hello world\n", ("text/plain", "file")),
        (b"\x00\x01\x02\xfe\xff", ("application/octet-stream", "file")),
    ],
    ids=["png", "jpeg", "gif", "pdf", "mp3", "mp4", "text", "binary"],
)
def test_sniff_mime_ignores_the_file_name(head: bytes, expected: tuple[str, str]) -> None:
    assert sniff_mime(head) == expected


def test_sniff_mime_truncated_utf8_tail_is_still_text() -> None:
    head = ("é" * 10).encode()[:-1]
    assert sniff_mime(head)[0] == "text/plain"


# ---------------------------------------------------------------- server lifecycle


async def test_start_and_close_are_idempotent(tmp_path: Path) -> None:
    config = make_config(tmp_path / "temp")
    handle = bridge_routes(config, FakeAuth({"tok-alice": "alice"}), FakeNotifier())
    await handle.start()
    await handle.start()
    await handle.close()
    await handle.close()
    assert {r.method for r in handle.routes} == {"GET", "POST", "PUT"}
    assert len(handle.routes) == 5
    paths = sorted(r.path for r in handle.routes)
    assert paths == [
        "/bridge/files/{token}/{name}",
        "/bridge/report",
        "/bridge/uploads",
        "/bridge/uploads/{upload_id}/chunks/{index}",
        "/bridge/uploads/{upload_id}/complete",
    ]
