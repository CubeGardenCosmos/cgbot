"""Upstream failure types surfaced to the agent loop (R1.5-T05).

Messages and ``body`` never contain the API key or the Authorization header; the adapter
scrubs the key from upstream bodies before constructing these.
"""

from __future__ import annotations


class ProviderError(Exception):
    """Base class for upstream failures. ``status`` is the HTTP status when one was received."""

    def __init__(self, message: str, *, status: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body  # truncated, credential-scrubbed upstream body excerpt


class RateLimited(ProviderError):
    """HTTP 429. ``retry_after`` is seconds from the Retry-After header, if provided."""

    def __init__(
        self,
        message: str,
        *,
        retry_after: float | None,
        status: int = 429,
        body: str = "",
    ) -> None:
        super().__init__(message, status=status, body=body)
        self.retry_after = retry_after


class UpstreamUnavailable(ProviderError):
    """5xx, network failure, timeout, or a stream that failed or ended before a terminal event."""


class BadRequest(ProviderError):
    """HTTP 4xx other than 429."""
