"""Simple in-memory rate limiting for FastAPI.

We intentionally avoid external dependencies (e.g. Redis) so this works
everywhere the API runs, and provides a circuit-breaker against runaway
request volume and paid LLM/API costs.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Deque

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

from archivum.config import Settings


def get_client_ip(request: Request) -> str:
    # If behind a proxy, prefer X-Forwarded-For.
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


@dataclass(frozen=True)
class RateLimitPolicy:
    limit: int
    window_seconds: int


class InMemoryRateLimiter:
    """Per-key sliding-window counter using a deque of timestamps."""

    def __init__(self) -> None:
        self._events: dict[str, Deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def check(self, key: str, policy: RateLimitPolicy) -> tuple[bool, int]:
        now = time.time()
        cutoff = now - policy.window_seconds

        async with self._lock:
            q = self._events[key]
            while q and q[0] <= cutoff:
                q.popleft()

            if len(q) >= policy.limit:
                retry_after = int(q[0] + policy.window_seconds - now) + 1
                return False, max(retry_after, 1)

            q.append(now)
            return True, 0


def rate_limit_policy_for_path(path: str, settings: Settings) -> RateLimitPolicy | None:
    if path.startswith("/api/auth/login"):
        return RateLimitPolicy(
            limit=settings.rate_limit_login_requests,
            window_seconds=settings.rate_limit_login_window_seconds,
        )

    # Its own bucket rather than the login bucket: deploy.md records that
    # scripted traffic through the Cloudflare tunnel trips the shared login
    # limiter, and pairing is exactly the scripted case.
    if path.startswith("/api/mcp/pairing/redeem"):
        return RateLimitPolicy(
            limit=settings.rate_limit_pairing_requests,
            window_seconds=settings.rate_limit_pairing_window_seconds,
        )

    # “Rate limit everything” under /api so paid/expensive endpoints
    # can’t be hammered.
    if path.startswith("/api/"):
        return RateLimitPolicy(
            limit=settings.rate_limit_api_requests,
            window_seconds=settings.rate_limit_api_window_seconds,
        )

    return None


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Apply per-IP rate limits for /api routes."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        super().__init__(app)
        self._settings = settings
        self._limiter = InMemoryRateLimiter()

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        # Avoid blocking browser preflight.
        if request.method == "OPTIONS":
            return await call_next(request)

        policy = rate_limit_policy_for_path(request.url.path, self._settings)
        if not policy:
            return await call_next(request)

        ip = get_client_ip(request)
        allowed, retry_after = await self._limiter.check(f"{ip}", policy)
        if allowed:
            return await call_next(request)

        return JSONResponse(
            status_code=429,
            content={"detail": "Too many requests"},
            headers={"Retry-After": str(retry_after)},
        )
