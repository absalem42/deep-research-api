"""Caller authentication, webhook signing, and rate limiting."""

from __future__ import annotations

import hashlib
import hmac
import time
from collections import deque
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status

from .config import Settings, get_settings

# --------------------------------------------------------------------------
# caller auth
# --------------------------------------------------------------------------

def _extract_key(authorization: str | None, x_api_key: str | None) -> str | None:
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer" and value.strip():
            return value.strip()
    if x_api_key and x_api_key.strip():
        return x_api_key.strip()
    return None


async def require_api_key(
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    settings: Annotated[Settings, Depends(get_settings)] = None,  # type: ignore[assignment]
) -> str:
    """Bearer token or X-API-Key. Compared in constant time."""
    if settings.auth_disabled:
        return "anonymous"

    allowed = settings.api_key_set
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No API keys configured; refusing to serve unauthenticated traffic.",
        )

    presented = _extract_key(authorization, x_api_key)
    if not presented:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing credentials. Send 'Authorization: Bearer <key>'.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # compare_digest against every key so timing does not leak which matched
    matched = False
    for candidate in allowed:
        if hmac.compare_digest(presented, candidate):
            matched = True
    if not matched:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key."
        )

    return hashlib.sha256(presented.encode()).hexdigest()[:16]


CallerId = Annotated[str, Depends(require_api_key)]


# --------------------------------------------------------------------------
# webhook signing
# --------------------------------------------------------------------------

def sign_payload(secret: str, body: bytes, timestamp: int | None = None) -> tuple[str, int]:
    """Stripe-style signature: HMAC over "<ts>.<body>".

    Including the timestamp inside the signed material is what stops an attacker
    replaying a captured body with a fresh header.
    """
    ts = timestamp if timestamp is not None else int(time.time())
    signed = f"{ts}.".encode() + body
    mac = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}", ts


def verify_signature(
    secret: str, body: bytes, header: str, tolerance_seconds: int = 300
) -> bool:
    """Receiver-side helper. Shipped in both client SDKs too."""
    parts = dict(
        piece.split("=", 1) for piece in header.split(",") if "=" in piece
    )
    try:
        ts = int(parts.get("t", ""))
    except ValueError:
        return False
    if abs(time.time() - ts) > tolerance_seconds:
        return False
    expected, _ = sign_payload(secret, body, ts)
    return hmac.compare_digest(expected, header)


# --------------------------------------------------------------------------
# rate limiting
# --------------------------------------------------------------------------

class SlidingWindowLimiter:
    """Per-caller sliding window.

    In-process on purpose: with more than one replica put the real limit at the
    ingress (Cloud Run / nginx / API gateway). This is a backstop that keeps a
    single runaway caller from exhausting the worker pool, not a billing control.
    """

    def __init__(self, limit: int, window_seconds: int) -> None:
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = {}

    def check(self, caller: str) -> tuple[bool, int, float]:
        now = time.monotonic()
        bucket = self._hits.setdefault(caller, deque())
        cutoff = now - self.window
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= self.limit:
            retry_after = self.window - (now - bucket[0])
            return False, 0, max(retry_after, 0.0)
        bucket.append(now)
        return True, self.limit - len(bucket), 0.0


def get_limiter(request: Request, settings: Settings) -> SlidingWindowLimiter:
    """One limiter per app instance, stored on app.state.

    Deliberately not a module-level global: that would be shared by every app
    built in the process, so counters would bleed across them.
    """
    limiter = getattr(request.app.state, "limiter", None)
    if limiter is None:
        limiter = SlidingWindowLimiter(
            settings.rate_limit_requests, settings.rate_limit_window_seconds
        )
        request.app.state.limiter = limiter
    return limiter


async def enforce_rate_limit(
    request: Request,
    caller: CallerId,
    settings: Annotated[Settings, Depends(get_settings)],
) -> str:
    limiter = get_limiter(request, settings)
    ok, remaining, retry_after = limiter.check(caller)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded.",
            headers={
                "Retry-After": str(int(retry_after) + 1),
                "X-RateLimit-Limit": str(settings.rate_limit_requests),
                "X-RateLimit-Remaining": "0",
            },
        )
    request.state.rate_remaining = remaining
    return caller


RateLimitedCaller = Annotated[str, Depends(enforce_rate_limit)]
