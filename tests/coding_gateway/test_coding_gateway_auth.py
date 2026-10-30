"""R5.1: sandbox HMAC tokens and bearer parsing (offline, deterministic)."""

from __future__ import annotations

import base64

import pytest

from cgbot.coding.auth import (
    TOKEN_PREFIX,
    WS_TOKEN_PREFIX,
    HmacSandboxAuth,
    bearer_token,
    derive_ws_token,
)
from cgbot.config.settings import Secret

SECRET = Secret("auth-test-secret-A")
OTHER = Secret("auth-test-secret-B")


def test_token_round_trips_to_user_id() -> None:
    auth = HmacSandboxAuth(SECRET)
    assert auth.resolve(auth.token_for("10001")) == "10001"


def test_token_carries_prefix() -> None:
    assert HmacSandboxAuth(SECRET).token_for("10001").startswith(TOKEN_PREFIX)


def test_token_is_stable_for_same_secret_and_user() -> None:
    assert HmacSandboxAuth(SECRET).token_for("42") == HmacSandboxAuth(SECRET).token_for("42")


def test_token_from_other_secret_is_rejected() -> None:
    token = HmacSandboxAuth(OTHER).token_for("10001")
    assert HmacSandboxAuth(SECRET).resolve(token) is None


def test_token_cannot_be_reattributed_to_another_user() -> None:
    auth = HmacSandboxAuth(SECRET)
    token = auth.token_for("10001")
    _, sig = token[len(TOKEN_PREFIX) :].split(".")
    forged_id = base64.urlsafe_b64encode(b"10002").rstrip(b"=").decode()
    assert auth.resolve(f"{TOKEN_PREFIX}{forged_id}.{sig}") is None


def test_flipped_signature_is_rejected() -> None:
    auth = HmacSandboxAuth(SECRET)
    token = auth.token_for("10001")
    head, sig = token.rsplit(".", 1)
    flipped = ("A" if sig[0] != "A" else "B") + sig[1:]
    assert auth.resolve(f"{head}.{flipped}") is None


@pytest.mark.parametrize(
    "token",
    [
        "",
        "garbage",
        "cgb-",
        "cgb-.",
        "cgb-MTAwMDE=.sig",  # padded base64 is not canonical
        "cgb-!!!.!!!",
        "x" * 600,
    ],
)
def test_malformed_tokens_are_rejected(token: str) -> None:
    assert HmacSandboxAuth(SECRET).resolve(token) is None


def test_ws_prefixed_token_is_not_a_sandbox_token() -> None:
    auth = HmacSandboxAuth(SECRET)
    ws = derive_ws_token(SECRET, "10001")
    assert ws.startswith(WS_TOKEN_PREFIX)
    assert auth.resolve(ws) is None


def test_ws_token_differs_from_sandbox_token_for_same_user() -> None:
    sandbox = HmacSandboxAuth(SECRET).token_for("10001")
    assert derive_ws_token(SECRET, "10001") != sandbox
    assert derive_ws_token(SECRET, "10001") != derive_ws_token(SECRET, "10002")


def test_empty_user_id_is_refused() -> None:
    with pytest.raises(ValueError):
        HmacSandboxAuth(SECRET).token_for("")


def test_empty_secret_is_refused() -> None:
    with pytest.raises(ValueError):
        HmacSandboxAuth(Secret("")).token_for("10001")


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("Bearer abc", "abc"),
        ("bearer abc", "abc"),
        ("BEARER abc", "abc"),
        ("Bearer   abc", "abc"),
        ("Basic abc", None),
        ("Bearer", None),
        ("Bearer ", None),
        ("", None),
        (None, None),
    ],
)
def test_bearer_token_parsing(header: str | None, expected: str | None) -> None:
    assert bearer_token(header) == expected
