"""Responses input-item helpers (R1.5).

``item_from_output`` is a documented pass-through: it copies a response output item into an
input item, dropping only the top-level ``id`` and ``status`` fields. Those reference stored
items, which do not exist with ``store=false``. Everything else (``call_id``, ``encrypted_content``,
nested content parts) is kept as the upstream sent it, so reasoning items replay unchanged.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from cgbot.provider.types import FunctionCall, OtherOutput, OutputItem, Reasoning, TextOutput

_DROPPED_KEYS = frozenset({"id", "status"})


def user_message(text: str) -> dict[str, Any]:
    return {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": text}],
    }


def function_call_output(call_id: str, output: str) -> dict[str, Any]:
    return {"type": "function_call_output", "call_id": call_id, "output": output}


def item_from_output(item: OutputItem | Mapping[str, Any]) -> dict[str, Any]:
    """Convert an output item (typed, or its raw wire mapping) into an input item.

    Only top-level ``id``/``status`` are removed; the rest is deep-copied as-is.
    """
    raw = _raw_of(item)
    return {k: copy.deepcopy(v) for k, v in raw.items() if k not in _DROPPED_KEYS}


def _raw_of(item: OutputItem | Mapping[str, Any]) -> Mapping[str, Any]:
    if isinstance(item, TextOutput | FunctionCall | Reasoning | OtherOutput):
        return item.raw
    return item
