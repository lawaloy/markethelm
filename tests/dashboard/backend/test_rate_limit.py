"""Production API rate-limit middleware tests."""

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

import dashboard.backend.rate_limit as rate_limit
from dashboard.backend.rate_limit import (
    RateLimitMiddleware,
    RateLimitRule,
    check_rate_limits,
    client_ip,
)
from src.storage.rate_limits import RateLimitUsage


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware)

    @app.get("/api/test")
    async def api_test():
        return {"ok": True}

    @app.get("/health")
    async def health():
        return {"ok": True}

    return app


def _api_request(path: str, method: str = "GET", peer: str = "203.0.113.5") -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": [],
            "client": (peer, 1234),
            "server": ("testserver", 80),
            "scheme": "http",
        }
    )


def _request(peer: str, forwarded: str = "") -> Request:
    headers = []
    if forwarded:
        headers.append((b"x-forwarded-for", forwarded.encode("ascii")))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/test",
            "raw_path": b"/api/test",
            "query_string": b"",
            "headers": headers,
            "client": (peer, 1234),
            "server": ("testserver", 80),
            "scheme": "http",
        }
    )


def _request_without_client(forwarded: str = "") -> Request:
    """ASGI scope with no ``client`` (unix sockets / some proxies)."""
    headers = []
    if forwarded:
        headers.append((b"x-forwarded-for", forwarded.encode("ascii")))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/test",
            "raw_path": b"/api/test",
            "query_string": b"",
            "headers": headers,
            "server": ("testserver", 80),
            "scheme": "http",
        }
    )


def test_middleware_returns_standard_limit_headers_and_429(monkeypatch) -> None:
    monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)
    monkeypatch.setenv("MARKETHELM_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setattr(
        rate_limit,
        "configured_rules",
        lambda: (RateLimitRule("test", 2, 60),),
    )
    monkeypatch.setattr(rate_limit, "_memory_counters", rate_limit._MemoryCounters())
    client = TestClient(_app())

    first = client.get("/api/test")
    second = client.get("/api/test")
    blocked = client.get("/api/test")

    assert first.status_code == 200
    assert first.headers["x-ratelimit-remaining"] == "1"
    assert second.headers["x-ratelimit-remaining"] == "0"
    assert blocked.status_code == 429
    assert blocked.json() == {"detail": "Too many requests."}
    assert int(blocked.headers["retry-after"]) >= 1


def test_non_api_routes_are_not_limited(monkeypatch) -> None:
    monkeypatch.setenv("MARKETHELM_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setattr(
        rate_limit,
        "configured_rules",
        lambda: (RateLimitRule("test", 1, 60),),
    )
    client = TestClient(_app())

    assert client.get("/health").status_code == 200
    assert client.get("/health").status_code == 200


def test_forwarded_header_ignored_from_untrusted_peer(monkeypatch) -> None:
    monkeypatch.setenv("MARKETHELM_TRUSTED_PROXY_CIDRS", "10.0.0.0/8")
    assert client_ip(_request("203.0.113.5", "198.51.100.7")) == "203.0.113.5"


def test_forwarded_chain_uses_first_untrusted_hop_from_right(monkeypatch) -> None:
    monkeypatch.setenv("MARKETHELM_TRUSTED_PROXY_CIDRS", "10.0.0.0/8,192.0.2.0/24")
    request = _request("10.0.0.5", "198.51.100.9, 192.0.2.10")
    assert client_ip(request) == "198.51.100.9"


def test_backend_failure_returns_503(monkeypatch) -> None:
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", "sqlite:///unused.db")
    monkeypatch.setattr(
        rate_limit,
        "configured_rules",
        lambda: (RateLimitRule("test", 1, 60),),
    )

    def fail(*args, **kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(rate_limit, "consume_rate_limit", fail)
    response = TestClient(_app()).get("/api/test")
    assert response.status_code == 503
    assert response.json() == {"detail": "Rate-limit service unavailable."}


def test_hosted_check_rate_limits_uses_database_usage_not_memory(monkeypatch) -> None:
    """Hosted limiting must apply consume_rate_limit's count to the decision.

    Multi-worker deploys share ``api_rate_limits``. The 503-on-failure test
    only proves consume is *called*; a process-local ``_MemoryCounters``
    fallback would still 429 in a single TestClient and still raise on
    consume errors. If the returned usage is ignored, the first request
    would be allowed from an empty memory bucket even when the database
    already counted past the limit.
    """

    monkeypatch.setenv("MARKETHELM_DATABASE_URL", "sqlite:///hosted.db")
    monkeypatch.setenv("MARKETHELM_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setattr(rate_limit, "_memory_counters", rate_limit._MemoryCounters())

    def fake_consume(key, *, now, window_seconds):
        return RateLimitUsage(count=3, reset_at=now + window_seconds)

    monkeypatch.setattr(rate_limit, "consume_rate_limit", fake_consume)
    rules = (RateLimitRule("test", 2, 60),)
    now = 1_700_000_000

    decision = check_rate_limits(
        _api_request("/api/test", peer="203.0.113.10"), now=now, rules=rules
    )

    assert decision is not None
    assert decision.allowed is False
    assert decision.limit == 2
    assert decision.remaining == 0
    assert decision.reset_at == now + 60


def test_invalid_enabled_value_falls_back_to_hosted_default(monkeypatch) -> None:
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", "sqlite:///hosted.db")
    monkeypatch.setenv("MARKETHELM_RATE_LIMIT_ENABLED", "maybe")
    assert rate_limit.rate_limiting_enabled() is True


def test_hosted_false_override_skips_limits_and_does_not_503(monkeypatch) -> None:
    """MARKETHELM_RATE_LIMIT_ENABLED=false must disable hosted limiting.

    Database mode turns limits on by default, and a broken consume_rate_limit
    fail-closes every /api/ request with 503. An explicit false override must
    skip consume so a limit-1 rule cannot 429 and a failing backend cannot 503.
    """
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", "sqlite:///hosted.db")
    monkeypatch.setenv("MARKETHELM_RATE_LIMIT_ENABLED", "false")
    monkeypatch.setattr(
        rate_limit,
        "configured_rules",
        lambda: (RateLimitRule("test", 1, 60),),
    )

    def fail(*args, **kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(rate_limit, "consume_rate_limit", fail)
    client = TestClient(_app())

    first = client.get("/api/test")
    second = client.get("/api/test")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == {"ok": True}
    assert second.json() == {"ok": True}
    assert "x-ratelimit-limit" not in first.headers
    assert "retry-after" not in first.headers
    assert "x-ratelimit-limit" not in second.headers


def test_invalid_forwarded_hop_falls_back_to_peer(monkeypatch) -> None:
    """Garbage X-Forwarded-For from a trusted proxy must not skip to a later hop."""
    monkeypatch.setenv("MARKETHELM_TRUSTED_PROXY_CIDRS", "10.0.0.0/8")
    assert client_ip(_request("10.0.0.5", "not-an-ip, 198.51.100.9")) == "10.0.0.5"


def test_poisoned_rate_limit_env_clamps_to_safe_bounds(monkeypatch) -> None:
    monkeypatch.setenv("MARKETHELM_RATE_LIMIT_REGISTER", "999999999")
    monkeypatch.setenv("MARKETHELM_RATE_LIMIT_LOGIN", "not-a-number")
    monkeypatch.setenv("MARKETHELM_RATE_LIMIT_GLOBAL", "0")
    monkeypatch.setenv("MARKETHELM_RATE_LIMIT_EXPENSIVE", "-5")
    monkeypatch.setenv("MARKETHELM_RATE_LIMIT_AUTH_EMAIL", "999999999")
    rules = {rule.name: rule for rule in rate_limit.configured_rules()}
    assert rules["auth-register"].limit == 1000
    assert rules["auth-login"].limit == 10
    assert rules["api-global"].limit == 1
    assert rules["expensive-write"].limit == 1
    assert rules["auth-email"].limit == 1000


def test_expensive_write_rule_covers_account_mutations() -> None:
    rules = {rule.name: rule for rule in rate_limit.configured_rules()}
    expensive = rules["expensive-write"]
    assert expensive.matches(_api_request("/api/auth/password/change", method="POST"))
    assert expensive.matches(_api_request("/api/auth/account", method="DELETE"))
    assert expensive.matches(_api_request("/api/alerts/test", method="POST"))
    assert expensive.matches(_api_request("/api/refresh/cancel", method="POST"))
    assert not expensive.matches(_api_request("/api/auth/account", method="GET"))
    assert not expensive.matches(_api_request("/api/alerts/quotes", method="GET"))
    assert not expensive.matches(_api_request("/api/refresh/status", method="GET"))


def test_auth_email_rule_covers_request_endpoints_not_confirm() -> None:
    rules = {rule.name: rule for rule in rate_limit.configured_rules()}
    auth_email = rules["auth-email"]
    assert auth_email.matches(_api_request("/api/auth/password-reset/request", method="POST"))
    assert auth_email.matches(_api_request("/api/auth/verify-email/request", method="POST"))
    assert not auth_email.matches(_api_request("/api/auth/password-reset/confirm", method="POST"))
    assert not auth_email.matches(_api_request("/api/auth/verify-email/confirm", method="POST"))
    assert not auth_email.matches(_api_request("/api/auth/password-reset/request", method="GET"))


def test_invalid_proxy_value_is_not_logged(monkeypatch, caplog) -> None:
    secret_value = "invalid-secret-proxy-value"
    monkeypatch.setenv("MARKETHELM_TRUSTED_PROXY_CIDRS", secret_value)
    assert client_ip(_request("203.0.113.5")) == "203.0.113.5"
    assert secret_value not in caplog.text


def test_invalid_proxy_cidr_is_skipped_without_disabling_valid_peers(
    monkeypatch,
) -> None:
    """One junk CIDR must not disable the rest of MARKETHELM_TRUSTED_PROXY_CIDRS.

    If ValueError aborted the whole parser, X-Forwarded-For would be ignored
    and every client behind the proxy would share the proxy's rate-limit bucket.
    """
    monkeypatch.setenv(
        "MARKETHELM_TRUSTED_PROXY_CIDRS",
        "not-a-cidr, 10.0.0.0/8, also-bad",
    )
    request = _request("10.0.0.5", "198.51.100.9")
    assert client_ip(request) == "198.51.100.9"


def test_rate_limit_buckets_are_isolated_by_client_ip(monkeypatch) -> None:
    """Dropping identity from the bucket key would make every client share one counter."""
    monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)
    monkeypatch.setenv("MARKETHELM_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setattr(rate_limit, "_memory_counters", rate_limit._MemoryCounters())
    rules = (RateLimitRule("test", 1, 60),)
    now = 1_700_000_000

    first = check_rate_limits(_api_request("/api/test", peer="203.0.113.10"), now=now, rules=rules)
    exhausted = check_rate_limits(
        _api_request("/api/test", peer="203.0.113.10"), now=now, rules=rules
    )
    other = check_rate_limits(_api_request("/api/test", peer="198.51.100.20"), now=now, rules=rules)

    assert first is not None and first.allowed is True
    assert exhausted is not None and exhausted.allowed is False
    assert other is not None and other.allowed is True


def test_empty_forwarded_header_from_trusted_proxy_uses_peer(monkeypatch) -> None:
    monkeypatch.setenv("MARKETHELM_TRUSTED_PROXY_CIDRS", "10.0.0.0/8")
    assert client_ip(_request("10.0.0.5", "")) == "10.0.0.5"


def test_all_trusted_hops_use_leftmost_forwarded_address(monkeypatch) -> None:
    monkeypatch.setenv("MARKETHELM_TRUSTED_PROXY_CIDRS", "10.0.0.0/8")
    request = _request("10.0.0.5", "10.0.0.9, 10.0.0.8")
    assert client_ip(request) == "10.0.0.9"


def test_ipv6_client_behind_trusted_ipv4_proxy(monkeypatch) -> None:
    """IPv6 clients behind an IPv4 hop must not share the proxy's rate-limit bucket."""
    monkeypatch.setenv("MARKETHELM_TRUSTED_PROXY_CIDRS", "10.0.0.0/8")
    request = _request("10.0.0.5", "2001:db8::9")
    assert client_ip(request) == "2001:db8::9"


def test_forwarded_header_ignored_from_untrusted_ipv6_peer(monkeypatch) -> None:
    monkeypatch.setenv("MARKETHELM_TRUSTED_PROXY_CIDRS", "2001:db8::/32")
    assert client_ip(_request("203.0.113.5", "2001:db8::9")) == "203.0.113.5"


def test_ipv6_forwarded_chain_uses_first_untrusted_hop(monkeypatch) -> None:
    monkeypatch.setenv("MARKETHELM_TRUSTED_PROXY_CIDRS", "2001:db8::/32,10.0.0.0/8")
    request = _request("10.0.0.5", "198.51.100.9, 2001:db8::10")
    assert client_ip(request) == "198.51.100.9"


def test_bracketed_ipv6_forwarded_hop_falls_back_to_peer(monkeypatch) -> None:
    """Bracketed X-Forwarded-For tokens are not valid IP literals; do not skip them."""
    monkeypatch.setenv("MARKETHELM_TRUSTED_PROXY_CIDRS", "10.0.0.0/8")
    assert client_ip(_request("10.0.0.5", "[2001:db8::9], 198.51.100.9")) == "10.0.0.5"


def test_rate_limit_buckets_are_isolated_by_ipv6_client_ip(monkeypatch) -> None:
    """IPv6 identities must hash separately so two clients cannot exhaust one bucket."""
    monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)
    monkeypatch.setenv("MARKETHELM_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setattr(rate_limit, "_memory_counters", rate_limit._MemoryCounters())
    rules = (RateLimitRule("test", 1, 60),)
    now = 1_700_000_000

    first = check_rate_limits(_api_request("/api/test", peer="2001:db8::10"), now=now, rules=rules)
    exhausted = check_rate_limits(
        _api_request("/api/test", peer="2001:db8::10"), now=now, rules=rules
    )
    other = check_rate_limits(_api_request("/api/test", peer="2001:db8::20"), now=now, rules=rules)

    assert first is not None and first.allowed is True
    assert exhausted is not None and exhausted.allowed is False
    assert other is not None and other.allowed is True


def test_memory_rate_limit_window_resets_after_expiry(monkeypatch) -> None:
    """File-mode / non-DB counters must start a fresh window after reset_at.

    ``consume_rate_limit`` already covers database windows. Self-host limiting
    uses ``_MemoryCounters``; if the bucket key drops ``window_start`` (or the
    window never advances), a limit-2 rule 429s the same client forever.
    """
    monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)
    monkeypatch.setenv("MARKETHELM_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setattr(rate_limit, "_memory_counters", rate_limit._MemoryCounters())
    rules = (RateLimitRule("test", 2, 60),)
    now = 1_700_000_000
    request = _api_request("/api/test", peer="203.0.113.10")

    first = check_rate_limits(request, now=now, rules=rules)
    second = check_rate_limits(request, now=now, rules=rules)
    blocked = check_rate_limits(request, now=now, rules=rules)

    assert first is not None and first.allowed is True
    assert first.remaining == 1
    assert second is not None and second.allowed is True
    assert second.remaining == 0
    assert blocked is not None and blocked.allowed is False
    assert blocked.reset_at == first.reset_at

    still_blocked = check_rate_limits(request, now=first.reset_at - 1, rules=rules)
    fresh = check_rate_limits(request, now=first.reset_at, rules=rules)

    assert still_blocked is not None and still_blocked.allowed is False
    assert fresh is not None and fresh.allowed is True
    assert fresh.remaining == 1
    assert fresh.reset_at == first.reset_at + 60


def test_missing_client_ignores_forwarded_header(monkeypatch) -> None:
    """An unparseable peer must not honor X-Forwarded-For.

    ``client_ip`` only trusts XFF when the socket peer is a real IP inside
    ``MARKETHELM_TRUSTED_PROXY_CIDRS``. ASGI scopes without ``client`` (unix
    sockets, some reverse-proxy setups) stringify to ``unknown``. If that
    ValueError path skipped ahead to the forwarded chain, anyone could mint a
    fresh identity per request and bypass rate limits.
    """
    monkeypatch.setenv("MARKETHELM_TRUSTED_PROXY_CIDRS", "10.0.0.0/8")
    request = _request_without_client("198.51.100.9")
    assert request.client is None
    assert client_ip(request) == "unknown"


def test_missing_client_shares_rate_limit_bucket(monkeypatch) -> None:
    """Requests without a socket peer must still consume one shared bucket.

    A random identity on missing ``client`` would let each call start a fresh
    limit-1 window and never 429. ``client_ip`` must stay stable so the
    middleware cannot be bypassed by omitting the ASGI client address.
    """
    monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)
    monkeypatch.setenv("MARKETHELM_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setattr(rate_limit, "_memory_counters", rate_limit._MemoryCounters())
    rules = (RateLimitRule("test", 1, 60),)
    now = 1_700_000_000

    first = check_rate_limits(_request_without_client(), now=now, rules=rules)
    second = check_rate_limits(_request_without_client(), now=now, rules=rules)

    assert first is not None and first.allowed is True
    assert first.remaining == 0
    assert second is not None and second.allowed is False
    assert second.remaining == 0
    assert second.limit == 1
