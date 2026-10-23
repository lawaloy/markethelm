"""GET/POST /api/alerts/quotes must drop sentinel tickers before Finnhub resolve."""

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


def test_get_quotes_drops_sentinel_and_path_like_symbols(monkeypatch):
    client = _client(monkeypatch)
    captured = _capture_resolve(monkeypatch)

    response = client.get(
        "/api/alerts/quotes",
        params={"symbols": "NAN,AAPL,INF,NONE,../ETC/PASSWD,MSFT"},
    )

    assert response.status_code == 200
    assert captured["symbols"] == ["AAPL", "MSFT"]
    assert captured["fetch_missing"] is True
    assert response.json()["prices"] == {"AAPL": 1.0, "MSFT": 1.0}


def test_post_quotes_drops_sentinel_and_path_like_symbols(monkeypatch):
    client = _client(monkeypatch)
    captured = _capture_resolve(monkeypatch)

    response = client.post(
        "/api/alerts/quotes",
        json={"symbols": ["nan", "AAPL", "Infinity", "AAPL/MSFT", "msft", "  "]},
    )

    assert response.status_code == 200
    assert captured["symbols"] == ["AAPL", "MSFT"]
    assert response.json()["prices"] == {"AAPL": 1.0, "MSFT": 1.0}


def test_get_quotes_all_sentinels_does_not_resolve(monkeypatch):
    """Poison-only query strings must not burn the shared quote budget."""
    client = _client(monkeypatch)
    captured = _capture_resolve(monkeypatch)

    response = client.get(
        "/api/alerts/quotes",
        params={"symbols": "NAN,INF,NONE,NULL,../ETC/PASSWD"},
    )

    assert response.status_code == 200
    assert response.json()["prices"] == {}
    assert "symbols" not in captured


def test_get_quotes_sentinels_do_not_consume_the_resolve_cap(monkeypatch):
    """Normalize before slicing so 15 NAN tokens cannot starve real tickers."""
    client = _client(monkeypatch)
    captured = _capture_resolve(monkeypatch)

    junk = ",".join(["NAN"] * 15)
    real = ",".join(f"SYM{i}" for i in range(20))
    response = client.get(
        "/api/alerts/quotes",
        params={"symbols": f"{junk},{real}"},
    )

    assert response.status_code == 200
    assert captured["symbols"] == [f"SYM{i}" for i in range(15)]


def test_post_quotes_all_sentinels_does_not_resolve(monkeypatch):
    """Poison-only POST bodies must not burn the shared quote budget."""
    client = _client(monkeypatch)
    captured = _capture_resolve(monkeypatch)

    response = client.post(
        "/api/alerts/quotes",
        json={"symbols": ["NAN", "INF", "NONE", "NULL", "../ETC/PASSWD", "  "]},
    )

    assert response.status_code == 200
    assert response.json()["prices"] == {}
    assert "symbols" not in captured


def test_post_quotes_sentinels_do_not_consume_the_resolve_cap(monkeypatch):
    """Normalize before slicing so 15 NAN tokens cannot starve real tickers."""
    client = _client(monkeypatch)
    captured = _capture_resolve(monkeypatch)

    junk = ["NAN"] * 15
    real = [f"SYM{i}" for i in range(20)]
    response = client.post(
        "/api/alerts/quotes",
        json={"symbols": junk + real},
    )

    assert response.status_code == 200
    assert captured["symbols"] == [f"SYM{i}" for i in range(15)]
    assert captured["fetch_missing"] is True
