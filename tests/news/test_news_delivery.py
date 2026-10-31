from __future__ import annotations

import base64
import io
import json
from pathlib import Path
from typing import Any

from PIL import Image

from cgbot.news import compose_push
from cgbot.news.delivery import (
    TIER_NAMES,
    build_issue_messages,
    read_delivery,
    slice_png,
)

DAY = "2026-11-01"
WIDTH = 40
TALL = 14000
BLANK_ROWS = (5500, 11200)  # the slicer must cut at these rows (limit 6000)


def _png(width: int, height: int, blank_rows: tuple[int, ...] = ()) -> bytes:
    stride = width * 3
    data = bytearray(height * stride)
    for y in range(height):
        for x in range(width):
            offset = y * stride + x * 3
            data[offset : offset + 3] = bytes(((x * 6) % 256, y % 256, 7))
    for y in blank_rows:
        data[y * stride : (y + 1) * stride] = b"\xff" * stride
    image = Image.frombytes("RGB", (width, height), bytes(data))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def _heights(pieces: list[bytes]) -> list[int]:
    heights: list[int] = []
    for piece in pieces:
        with Image.open(io.BytesIO(piece)) as opened:
            heights.append(opened.size[1])
    return heights


def _day_dir(tmp_path: Path, payload: dict[str, Any], files: dict[str, bytes]) -> Path:
    output = tmp_path / "output"
    day_dir = output / DAY
    day_dir.mkdir(parents=True)
    (day_dir / "delivery.json").write_text(json.dumps(payload), encoding="utf-8")
    for name, content in files.items():
        (day_dir / name).write_bytes(content)
    return output


def _payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "issue_id": "2026-W44",
        "status": "complete",
        "tiers": {
            "fast": {"path": "fast.png", "label_zh": "快讯图", "error": None},
            "lite": {
                "path": "lite.pdf",
                "file_name": "digest.pdf",
                "label_zh": "精简版",
                "error": None,
            },
            "max": {"link": "https://oss.example/max", "label_zh": "完整版", "error": None},
        },
    }
    payload.update(overrides)
    return payload


def test_slice_cuts_at_blank_rows_and_keeps_all_rows() -> None:
    data = _png(WIDTH, TALL, blank_rows=BLANK_ROWS)

    pieces = slice_png(data, max_height=6000)

    assert _heights(pieces) == [5500, 5700, 2800]
    assert sum(_heights(pieces)) == TALL


def test_slice_returns_small_image_unchanged() -> None:
    data = _png(WIDTH, 120)

    assert slice_png(data, max_height=6000) == [data]


def test_read_delivery_missing_day_is_none(tmp_path: Path) -> None:
    assert read_delivery(tmp_path, DAY) is None


def test_read_delivery_malformed_json_is_none(tmp_path: Path) -> None:
    day_dir = tmp_path / DAY
    day_dir.mkdir()
    (day_dir / "delivery.json").write_text("{not json", encoding="utf-8")

    assert read_delivery(tmp_path, DAY) is None


def test_read_delivery_parses_complete_issue(tmp_path: Path) -> None:
    output = _day_dir(tmp_path, _payload(), {"fast.png": _png(WIDTH, 50), "lite.pdf": b"%PDF"})

    delivery = read_delivery(output, DAY)

    assert delivery is not None
    assert delivery.issue_id == "2026-W44"
    assert delivery.complete is True
    assert tuple(tier.name for tier in delivery.tiers) == TIER_NAMES


def test_issue_id_falls_back_to_day_and_status_gates_complete(tmp_path: Path) -> None:
    payload = _payload(status="partial")
    del payload["issue_id"]
    output = _day_dir(tmp_path, payload, {})

    delivery = read_delivery(output, DAY)

    assert delivery is not None
    assert delivery.issue_id == DAY
    assert delivery.complete is False


def test_build_all_tiers_for_small_fixture(tmp_path: Path) -> None:
    output = _day_dir(
        tmp_path,
        _payload(),
        {"fast.png": _png(WIDTH, 50), "lite.pdf": b"%PDF-1.4 test"},
    )
    delivery = read_delivery(output, DAY)
    assert delivery is not None

    built = build_issue_messages(delivery, TIER_NAMES)

    assert built.notes == ()
    assert len(built.groups) == 3
    fast, lite, max_link = built.groups
    assert fast[0].data == {"text": "快讯图"}
    assert fast[1].type == "image"
    assert str(fast[1].data["file"]).startswith("base64://")
    assert base64.b64decode(str(fast[1].data["file"]).removeprefix("base64://"))[:8] == (
        b"\x89PNG\r\n\x1a\n"
    )
    assert lite[1].type == "file"
    assert lite[1].data["name"] == "digest.pdf"
    assert base64.b64decode(str(lite[1].data["file"]).removeprefix("base64://")) == b"%PDF-1.4 test"
    assert "https://oss.example/max" in str(max_link[0].data["text"])
    assert "24 小时内有效" in str(max_link[0].data["text"])


def test_tall_fast_image_becomes_labelled_slices(tmp_path: Path) -> None:
    output = _day_dir(tmp_path, _payload(), {"fast.png": _png(WIDTH, TALL, BLANK_ROWS)})
    delivery = read_delivery(output, DAY)
    assert delivery is not None

    built = build_issue_messages(delivery, ("fast",))

    assert [group[0].data["text"] for group in built.groups] == [
        "快讯图(1/3)",
        "快讯图(2/3)",
        "快讯图(3/3)",
    ]


def test_error_and_missing_tiers_become_notes(tmp_path: Path) -> None:
    payload = _payload()
    payload["tiers"]["fast"]["error"] = "render failed"
    payload["tiers"]["lite"]["path"] = "missing.pdf"
    output = _day_dir(tmp_path, payload, {})
    delivery = read_delivery(output, DAY)
    assert delivery is not None

    built = build_issue_messages(delivery, TIER_NAMES)

    assert [group[0].data["text"] for group in built.groups] == [
        "完整版(链接 24 小时内有效):\nhttps://oss.example/max"
    ]
    assert len(built.notes) == 2
    assert "render failed" in built.notes[0]


def test_compose_push_partial_adds_note_and_apology_when_nothing_usable(tmp_path: Path) -> None:
    output = _day_dir(
        tmp_path,
        _payload(status="running"),
        {"fast.png": _png(WIDTH, 50)},
    )
    delivery = read_delivery(output, DAY)
    assert delivery is not None

    partial = compose_push(delivery, "2026-W44")
    assert str(partial[-1][0].data["text"]).startswith("（今天的刊还在生成中")

    empty_day = _day_dir(tmp_path / "empty", _payload(status="running", tiers={}), {})
    apology = compose_push(read_delivery(empty_day, DAY), "2026-W44")
    assert len(apology) == 1
    assert "没能按时出来" in str(apology[0][0].data["text"])
