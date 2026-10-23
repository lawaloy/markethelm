"""GET /api/alerts/quotes must cap symbol fan-out before Finnhub resolve (like POST)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from dashboard.backend.main import app


def test_get_quotes_caps_symbols_before_resolving(monkeypatch):
    client = TestClient(app)
    monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)

    captured = {}

    def fake_resolve(symbols, fetch_missing=True):
        captured["symbols"] = list(symbols)
        captured["fetch_missing"] = fetch_missing
        return {}

    monkeypatch.setattr(
        "dashboard.backend.api.alerts.resolve_symbol_prices",
        fake_resolve,
    )

    symbols = ",".join(f"SYM{i}" for i in range(20))
    r = client.get("/api/alerts/quotes", params={"symbols": symbols})

    assert r.status_code == 200
    assert captured["symbols"] == [f"SYM{i}" for i in range(15)]
    assert captured["fetch_missing"] is True
