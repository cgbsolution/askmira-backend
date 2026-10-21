"""Abuse protection for the unauthenticated careers, portal and scheduling routes.

These routes have no login by design — a candidate has no account. That makes
them the cheapest place in the product to hammer: an apply form that accepts a
thousand submissions a minute fills a recruiter's pipeline with junk, and a
portal token endpoint with no throttle is a token-guessing oracle.

**What this is.** A sliding-window limit per client address and bucket, held in
this process's memory, plus a honeypot check. It stops a single script from a
single address.

**What this is not.** It is per process: behind several API workers each keeps
its own count, and it resets on restart. It reads the socket's peer address and
deliberately ignores `X-Forwarded-For`, which any caller can set — behind a
reverse proxy every request then shares the proxy's address, so the proxy must
also rate-limit. A distributed limiter needs a shared store this deployment does
not have, and the report says so rather than this module pretending otherwise.
"""

from __future__ import annotations

import time
from collections import OrderedDict, deque
from dataclasses import dataclass

from fastapi import HTTPException, Request, status

__all__ = [
    "APPLY",
    "BOOKING",
    "DOCUMENT_UPLOAD",
    "LIMITER",
    "OAUTH_REGISTER",
    "OFFER_RESPONSE",
    "PORTAL_READ",
    "RESCHEDULE_REQUEST",
    "SCHEDULE_READ",
    "WHATSAPP_CHOICE",
    "Limit",
    "RateLimiter",
    "client_address",
    "refuse_honeypot",
    "throttle",
]


@dataclass(frozen=True, slots=True)
class Limit:
    bucket: str
    limit: int
    window_seconds: int


APPLY = Limit("apply", 5, 600)
PORTAL_READ = Limit("portal_read", 60, 600)
DOCUMENT_UPLOAD = Limit("document_upload", 10, 3600)
RESCHEDULE_REQUEST = Limit("reschedule_request", 5, 3600)
SCHEDULE_READ = Limit("schedule_read", 60, 600)
BOOKING = Limit("booking", 10, 600)
OFFER_RESPONSE = Limit("offer_response", 10, 3600)
WHATSAPP_CHOICE = Limit("whatsapp_choice", 10, 3600)
OAUTH_REGISTER = Limit("oauth_register", 20, 3600)


class RateLimiter:
    """Sliding-window counts, bounded in memory."""

    def __init__(self, *, max_keys: int = 50_000) -> None:
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()
        self._max_keys = max_keys

    def reset(self) -> None:
        self._hits.clear()

    def hit(self, key: str, *, limit: int, window_seconds: int, now: float | None = None) -> int:
        """Record a hit. Returns 0 when allowed, else seconds until one is."""
        moment = time.monotonic() if now is None else now
        window = self._hits.get(key)
        if window is None:
            window = deque()
            self._hits[key] = window
            while len(self._hits) > self._max_keys:
                self._hits.popitem(last=False)
        else:
            self._hits.move_to_end(key)

        cutoff = moment - window_seconds
        while window and window[0] <= cutoff:
            window.popleft()
        if len(window) >= limit:
            return max(1, int(window[0] + window_seconds - moment) + 1)
        window.append(moment)
        return 0


LIMITER = RateLimiter()


def client_address(request: Request) -> str:
    """The socket peer. Forwarded headers are caller-controlled and ignored."""
    return request.client.host if request.client else "unknown"


def throttle(request: Request, limit: Limit, *, scope: str = "") -> None:
    """Refuse with 429 when this address has used up the bucket."""
    key = f"{limit.bucket}|{client_address(request)}|{scope}"
    retry_after = LIMITER.hit(key, limit=limit.limit, window_seconds=limit.window_seconds)
    if retry_after:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                "Too many attempts from this connection. Please wait a few minutes and try again."
            ),
            headers={"Retry-After": str(retry_after)},
        )


def refuse_honeypot(value: str | None) -> None:
    """A field no person can see, so no person fills it in."""
    if value and value.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This submission could not be accepted.",
        )
