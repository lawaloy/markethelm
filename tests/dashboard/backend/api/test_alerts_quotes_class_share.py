"""GET/POST /api/alerts/quotes must keep class-share tickers like BRK.B."""

from __future__ import annotations

from fastapi.testclient import TestClient

from dashboard.backend.main import app


def _client(monkeypatch):
    monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)
    return TestClient(app)


def _capture_resolve(monkeypatch):
    captured = {}

    def fake_resolve(symbols, fetch_missing=True):
        captured["symbols"] = list(symbols)
        captured["fetch_missing"] = fetch_missing
        return {symbol: 1.0 for symbol in symbols}

    monkeypatch.setattr(
        "dashboard.backend.api.alerts.resolve_symbol_prices",
        fake_resolve,
    )
    return captured


def test_get_quotes_keeps_class_share_tickers(monkeypatch):
    """Dots/hyphens are valid US share-class tickers, not path-like junk.

    Tightening the quotes parser to ``[A-Z]+`` would drop Berkshire and
    Brown-Forman while sentinel tests still passed.
    """
    client = _client(monkeypatch)
    captured = _capture_resolve(monkeypatch)

    response = client.get(
        "/api/alerts/quotes",
        params={"symbols": "BRK.B,BF-B,AAPL"},
    )

    assert response.status_code == 200
    assert captured["symbols"] == ["BRK.B", "BF-B", "AAPL"]
    assert captured["fetch_missing"] is True
    assert response.json()["prices"] == {"BRK.B": 1.0, "BF-B": 1.0, "AAPL": 1.0}


def test_post_quotes_keeps_padded_class_share_tickers(monkeypatch):
    client = _client(monkeypatch)
    captured = _capture_resolve(monkeypatch)

    response = client.post(
        "/api/alerts/quotes",
        json={"symbols": [" brk.b ", "BF-B", "aapl"]},
    )

    assert response.status_code == 200
    assert captured["symbols"] == ["BRK.B", "BF-B", "AAPL"]
    assert captured["fetch_missing"] is True
    assert response.json()["prices"] == {"BRK.B": 1.0, "BF-B": 1.0, "AAPL": 1.0}
