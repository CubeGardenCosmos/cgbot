"""Stateless HMAC bearer tokens for sandboxes (contracts.SandboxAuth) and sandbox ws tokens.

Sandbox token: ``cgb-<b64url(user_id)>.<b64url(HMAC-SHA256(secret, "sandbox:" + user_id))>``.
The user id travels inside the token, so :meth:`HmacSandboxAuth.resolve` recomputes the MAC
and compares it in constant time; nothing is stored, so a bot restart keeps live sandboxes valid.

Websocket capability token (for the app-server connection, sandbox B): ``cgw-<b64url(HMAC(secret,
"ws:" + user_id))>``. It is never accepted as a gateway/bridge bearer token: the labels differ and
the prefixes differ.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
from typing import Final

from cgbot.config.settings import Secret

TOKEN_PREFIX: Final = "cgb-"
WS_TOKEN_PREFIX: Final = "cgw-"
_SANDBOX_LABEL: Final = b"sandbox:"
_WS_LABEL: Final = b"ws:"
_MAX_TOKEN_CHARS: Final = 512
_B64URL: Final = re.compile(r"[A-Za-z0-9_-]+")


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes | None:
    """Strict unpadded base64url decode; returns None unless the text is canonical."""
    if _B64URL.fullmatch(text) is None:
        return None
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except binascii.Error:
        return None
    return raw if _b64(raw) == text else None


def _mac(key: bytes, label: bytes, user_id: str) -> bytes:
    return hmac.new(key, label + user_id.encode("utf-8"), hashlib.sha256).digest()


def _key_bytes(secret: Secret) -> bytes:
    key = secret.reveal().encode("utf-8")
    if not key:
        raise ValueError("HMAC secret must be non-empty")
    return key


def _check_user_id(user_id: str) -> None:
    if not isinstance(user_id, str) or not user_id:
        raise ValueError("user_id must be a non-empty string")


class HmacSandboxAuth:
    """Implements ``contracts.SandboxAuth`` with HMAC-SHA256 derived, self-describing tokens."""

    __slots__ = ("_key",)

    def __init__(self, secret: Secret) -> None:
        self._key = _key_bytes(secret)

    def token_for(self, user_id: str) -> str:
        _check_user_id(user_id)
        mac = _mac(self._key, _SANDBOX_LABEL, user_id)
        return f"{TOKEN_PREFIX}{_b64(user_id.encode('utf-8'))}.{_b64(mac)}"

    def resolve(self, token: str) -> str | None:
        """Return the user id a valid token was issued for; None for anything else."""
        if not isinstance(token, str) or len(token) > _MAX_TOKEN_CHARS:
            return None
        if not token.startswith(TOKEN_PREFIX):
            return None
        id_part, sep, mac_part = token[len(TOKEN_PREFIX) :].partition(".")
        if not sep or "." in mac_part:
            return None
        id_raw = _unb64(id_part)
        mac_raw = _unb64(mac_part)
        if id_raw is None or mac_raw is None:
            return None
        try:
            user_id = id_raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
        if not user_id:
            return None
        if not hmac.compare_digest(mac_raw, _mac(self._key, _SANDBOX_LABEL, user_id)):
            return None
        return user_id


def derive_ws_token(secret: Secret, user_id: str) -> str:
    """Deterministic app-server websocket capability token for ``user_id`` (sandbox B)."""
    _check_user_id(user_id)
    return WS_TOKEN_PREFIX + _b64(_mac(_key_bytes(secret), _WS_LABEL, user_id))


def bearer_token(authorization: str | None) -> str | None:
    """Extract the token from an ``Authorization: Bearer <token>`` header value."""
    if not authorization:
        return None
    scheme, _, rest = authorization.strip().partition(" ")
    token = rest.strip()
    if scheme.lower() != "bearer" or not token:
        return None
    return token
