"""Production API rate limiting for hosted and local deployments."""

from __future__ import annotations

import hashlib
import ipaddress
import os
import threading
import time
from dataclasses import dataclass
from typing import Dict, Iterable, Optional, Tuple

from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from src.core.logger import get_logger
from src.storage.database import database_enabled
from src.storage.rate_limits import RateLimitUsage, consume_rate_limit

logger = get_logger(__name__)


@dataclass(frozen=True)
class RateLimitRule:
    name: str
    limit: int
    window_seconds: int
    paths: Tuple[str, ...] = ()
    methods: Tuple[str, ...] = ()

    def matches(self, request: Request) -> bool:
        if self.paths and request.url.path not in self.paths:
            return False
        return not self.methods or request.method.upper() in self.methods


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    limit: int
    remaining: int
    reset_at: int


def _bounded_env(name: str, default: int, *, maximum: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return max(1, min(int(raw), maximum))
    except ValueError:
        logger.warning("Invalid %s; using %s", name, default)
        return default


def rate_limiting_enabled() -> bool:
    raw = (os.environ.get("MARKETHELM_RATE_LIMIT_ENABLED") or "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    if raw:
        logger.warning(
            "Invalid MARKETHELM_RATE_LIMIT_ENABLED; using database-mode default",
        )
    return database_enabled()


def configured_rules() -> Tuple[RateLimitRule, ...]:
    return (
        RateLimitRule(
            "auth-register",
            _bounded_env("MARKETHELM_RATE_LIMIT_REGISTER", 5, maximum=1000),
            3600,
            paths=("/api/auth/register",),
            methods=("POST",),
        ),
        RateLimitRule(
            "auth-login",
            _bounded_env("MARKETHELM_RATE_LIMIT_LOGIN", 10, maximum=10000),
            60,
            paths=("/api/auth/login",),
            methods=("POST",),
        ),
        RateLimitRule(
            "auth-email",
            _bounded_env("MARKETHELM_RATE_LIMIT_AUTH_EMAIL", 5, maximum=1000),
            3600,
            paths=(
                "/api/auth/password-reset/request",
                "/api/auth/verify-email/request",
            ),
            methods=("POST",),
        ),
        RateLimitRule(
            "expensive-write",
            _bounded_env("MARKETHELM_RATE_LIMIT_EXPENSIVE", 10, maximum=10000),
            60,
            paths=(
                "/api/refresh",
                "/api/refresh/cancel",
                "/api/alerts/run",
                "/api/alerts/test",
                "/api/alerts/quotes",
                "/api/auth/password/change",
                "/api/auth/account",
            ),
            # DELETE covers /api/auth/account; GET quotes stay on the global bucket.
            methods=("POST", "DELETE"),
        ),
        RateLimitRule(
            "api-global",
            _bounded_env("MARKETHELM_RATE_LIMIT_GLOBAL", 120, maximum=100000),
            60,
        ),
    )


def _trusted_proxy_networks() -> Tuple[ipaddress._BaseNetwork, ...]:
    networks = []
    for raw in (os.environ.get("MARKETHELM_TRUSTED_PROXY_CIDRS") or "").split(","):
        value = raw.strip()
        if not value:
            continue
        try:
            networks.append(ipaddress.ip_network(value, strict=False))
        except ValueError:
            logger.warning("Ignoring invalid trusted proxy CIDR entry")
    return tuple(networks)


def client_ip(request: Request) -> str:
    peer = request.client.host if request.client else "unknown"
    try:
        peer_address = ipaddress.ip_address(peer)
    except ValueError:
        return peer
    networks = _trusted_proxy_networks()
    if not any(peer_address in network for network in networks):
        return peer
    chain = []
    for raw in request.headers.get("x-forwarded-for", "").split(","):
        value = raw.strip()
        if not value:
            continue
        try:
            chain.append(ipaddress.ip_address(value))
        except ValueError:
            return peer
    chain.append(peer_address)
    for address in reversed(chain):
        if not any(address in network for network in networks):
            return str(address)
    return str(chain[0])


class _MemoryCounters:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: Dict[Tuple[str, int], Tuple[int, int]] = {}

    def consume(self, key: str, now: int, window_seconds: int) -> RateLimitUsage:
        window_start = now - (now % window_seconds)
        reset_at = window_start + window_seconds
        with self._lock:
            self._counters = {
                bucket: value for bucket, value in self._counters.items() if value[1] > now
            }
            bucket = (key, window_start)
            count = self._counters.get(bucket, (0, reset_at))[0] + 1
            self._counters[bucket] = (count, reset_at)
        return RateLimitUsage(count=count, reset_at=reset_at)


_memory_counters = _MemoryCounters()


def _bucket_key(rule: RateLimitRule, identity: str) -> str:
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"{rule.name}:{digest}"


def check_rate_limits(
    request: Request,
    *,
    now: Optional[int] = None,
    rules: Optional[Iterable[RateLimitRule]] = None,
) -> Optional[RateLimitDecision]:
    if not request.url.path.startswith("/api/") or not rate_limiting_enabled():
        return None
    timestamp = int(time.time()) if now is None else now
    identity = client_ip(request)
    decisions = []
    for rule in rules or configured_rules():
        if not rule.matches(request):
            continue
        key = _bucket_key(rule, identity)
        usage = (
            consume_rate_limit(
                key,
                now=timestamp,
                window_seconds=rule.window_seconds,
            )
            if database_enabled()
            else _memory_counters.consume(key, timestamp, rule.window_seconds)
        )
        decisions.append(
            RateLimitDecision(
                allowed=usage.count <= rule.limit,
                limit=rule.limit,
                remaining=max(0, rule.limit - usage.count),
                reset_at=usage.reset_at,
            )
        )
    if not decisions:
        return None
    blocked = next((decision for decision in decisions if not decision.allowed), None)
    return blocked or min(decisions, key=lambda decision: decision.remaining)


def _headers(decision: RateLimitDecision, now: int) -> Dict[str, str]:
    headers = {
        "X-RateLimit-Limit": str(decision.limit),
        "X-RateLimit-Remaining": str(decision.remaining),
        "X-RateLimit-Reset": str(decision.reset_at),
    }
    if not decision.allowed:
        headers["Retry-After"] = str(max(1, decision.reset_at - now))
    return headers


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        now = int(time.time())
        try:
            decision = await run_in_threadpool(check_rate_limits, request, now=now)
        except Exception as exc:
            logger.exception("Rate-limit backend failed: %s", exc)
            return JSONResponse(
                status_code=503,
                content={"detail": "Rate-limit service unavailable."},
            )
        if decision is not None and not decision.allowed:
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests."},
                headers=_headers(decision, now),
            )
        response = await call_next(request)
        if decision is not None:
            response.headers.update(_headers(decision, now))
        return response
