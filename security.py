"""Minimal public-Beta HTTP protections."""
from __future__ import annotations

import time
import logging
import secrets
import os
from collections import defaultdict, deque

from fastapi import Request
from fastapi.responses import JSONResponse

WINDOW_SECONDS = 60
LIMITS = {
    "/api/parse_resume": 12,
    "/api/submissions": 10,
    "/api/admin/question-imports": 6,
    "/api/admin/": 30,
    "/api/account/": 8,
}
_requests: dict[tuple[str, str], deque[float]] = defaultdict(deque)
logger = logging.getLogger("interviewradar.http")


def _limit_for(path: str) -> tuple[str, int] | None:
    # Authenticated read requests run on every page render. Limit only
    # credential mutations, otherwise a normal dashboard refresh pattern can
    # create an artificial production 429.
    if path in {"/api/auth/login", "/api/auth/register"}:
        return ("/api/auth/credentials", 12)
    if path == "/api/sessions":
        return ("/api/sessions/create", 20)
    if path.startswith("/api/sessions/") and path.endswith("/answer"):
        return ("/api/sessions/answer", 40)
    if path.startswith("/api/sessions/") and path.endswith("/transcribe"):
        return ("/api/sessions/transcribe", 12)
    if path.startswith("/api/sessions/") and (path.endswith("/feedback") or path.endswith("/report/export")):
        return ("/api/sessions/user-actions", 20)
    for prefix, limit in LIMITS.items():
        if path.startswith(prefix):
            return prefix, limit
    return None


def _is_production() -> bool:
    return os.environ.get("APP_ENV", "development").strip().lower() == "production"


def _client_key(request: Request) -> str:
    """Use the client IP supplied by the only exposed reverse proxy in production.

    Docker Compose exposes Nginx but not the FastAPI container. Nginx overwrites
    `X-Real-IP`, so using it here avoids a single internal proxy address sharing
    every public user's rate-limit bucket. Local development deliberately uses
    the direct peer address, where headers are easy to spoof.
    """
    if _is_production():
        real_ip = request.headers.get("x-real-ip", "").strip()
        if real_ip:
            return real_ip[:128]
    return request.client.host if request.client else "unknown"


async def protect_public_beta(request: Request, call_next):
    request_id = secrets.token_hex(6)
    started = time.perf_counter()
    rule = _limit_for(request.url.path)
    # Local development and automated feature checks commonly perform many
    # rapid calls from one loopback peer. The public protection is for the
    # production Nginx boundary; disabling it locally avoids turning normal
    # product testing into artificial 429 failures.
    if rule and _is_production():
        bucket, limit = rule
        client = _client_key(request)
        key = (client, bucket)
        now = time.monotonic()
        entries = _requests[key]
        while entries and now - entries[0] >= WINDOW_SECONDS:
            entries.popleft()
        if len(entries) >= limit:
            response = JSONResponse({"detail": "请求过于频繁，请稍后再试"}, status_code=429)
            response.headers["X-Request-ID"] = request_id
            logger.warning("request_id=%s method=%s path=%s status=429 duration_ms=%d", request_id, request.method, request.url.path, int((time.perf_counter() - started) * 1000))
            return response
        entries.append(now)
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; media-src 'self' blob:; "
        "script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; frame-ancestors 'none'"
    )
    logger.info("request_id=%s method=%s path=%s status=%d duration_ms=%d", request_id, request.method, request.url.path, response.status_code, int((time.perf_counter() - started) * 1000))
    return response
