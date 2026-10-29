"""Per-application request rate limits.

See docs/20-applications-api-keys.md. A sliding window of one minute on the injected clock:
an application may start at most ``requests_per_minute`` requests in any 60 s. Memory is
bounded by the limit: at most that many timestamps are kept per application.
"""

from __future__ import annotations

from collections import deque
from typing import Protocol

from gateway.clock import Clock

WINDOW_MS = 60_000.0


class RateLimitedError(Exception):
    def __init__(self, application_id: str, limit: int, retry_after_ms: float) -> None:
        self.application_id = application_id
        self.limit = limit
        self.retry_after_ms = retry_after_ms
        super().__init__(
            f"Application {application_id!r} may send at most {limit} requests a minute."
        )


class RateLimiter(Protocol):
    """Requests per minute per application (docs/34-state-interfaces.md)."""

    async def admit(self, application_id: str, requests_per_minute: int | None) -> None:
        """Count one request, or raise ``RateLimitedError`` (which counts nothing)."""
        ...


class RequestRateLimiter:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._starts: dict[str, deque[float]] = {}

    async def admit(self, application_id: str, requests_per_minute: int | None) -> None:
        """Count one request, or raise ``RateLimitedError`` (which counts nothing)."""
        if requests_per_minute is None:
            return
        now = self._clock.now_ms()
        starts = self._starts.setdefault(application_id, deque())
        while starts and starts[0] <= now - WINDOW_MS:
            starts.popleft()
        while len(starts) > requests_per_minute:  # the limit was lowered
            starts.popleft()
        if len(starts) >= requests_per_minute:
            raise RateLimitedError(application_id, requests_per_minute, starts[0] + WINDOW_MS - now)
        starts.append(now)
