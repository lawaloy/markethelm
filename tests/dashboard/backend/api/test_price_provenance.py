"""Saved quote provenance survives the API in file and hosted storage modes."""

from datetime import datetime
from unittest.mock import patch

from fastapi.testclient import TestClient

from dashboard.backend.main import app
from dashboard.backend.services.data_loader import DataLoader
from src.storage.market_bars import latest_saved_quotes, upsert_market_bars


def test_market_overview_exposes_quote_time_range(tmp_path, monkeypatch):
    monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)
    upsert_market_bars(
        [
            {
                "symbol": "AAPL",
                "close": 150,
                "change_percent": 1,
                "quote_timestamp": "2026-10-02T15:07:00+00:00",
            },
            {
                "symbol": "MSFT",
                "close": 350,
                "change_percent": -1,
                "quote_timestamp": "2026-10-02T15:08:00+00:00",
            },
        ],
        "2026-10-02",
        data_dir=tmp_path,
    )
    with patch("dashboard.backend.api.market.get_data_loader", return_value=DataLoader(tmp_path)):
        response = TestClient(app).get("/api/market/overview")
    assert response.status_code == 200
    assert response.json()["quoteTimeStart"] == "2026-10-02T15:07:00+00:00"
    assert response.json()["quoteTimeEnd"] == "2026-10-02T15:08:00+00:00"


def test_market_overview_omits_quote_time_range_when_any_bar_lacks_tz(tmp_path, monkeypatch):
    """Headline freshness is all-or-nothing: one naive/missing stamp blanks the range."""
    monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)
    upsert_market_bars(
        [
            {
                "symbol": "AAPL",
                "close": 150,
                "change_percent": 1,
                "quote_timestamp": "2026-10-02T15:07:00+00:00",
            },
            {
                "symbol": "MSFT",
                "close": 350,
                "change_percent": -1,
                "quote_timestamp": "2026-10-02T15:08:00",
            },
        ],
        "2026-10-02",
        data_dir=tmp_path,
    )
    with patch("dashboard.backend.api.market.get_data_loader", return_value=DataLoader(tmp_path)):
        response = TestClient(app).get("/api/market/overview")
    assert response.status_code == 200
    payload = response.json()
    assert payload["quoteTimeStart"] is None
    assert payload["quoteTimeEnd"] is None


def test_alert_catalog_and_quote_return_newest_provider_time(tmp_path, monkeypatch):
    monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)
    upsert_market_bars(
        [{"symbol": "AAPL", "close": 100, "quote_timestamp": "2026-10-02T15:07:00+00:00"}],
        "2026-10-02",
        data_dir=tmp_path,
    )
    upsert_market_bars(
        [{"symbol": "AAPL", "close": 101, "quote_timestamp": "2026-10-02T20:00:00+00:00"}],
        "2026-10-03",
        data_dir=tmp_path,
    )
    loader = DataLoader(tmp_path)
    with (
        patch(
            "dashboard.backend.api.alerts.build_symbol_catalog",
            return_value=(["AAPL"], {"AAPL": "Apple"}),
        ),
        patch("dashboard.backend.api.alerts.get_data_loader", return_value=loader),
        patch("dashboard.backend.services.data_loader.get_data_loader", return_value=loader),
    ):
        client = TestClient(app)
        catalog = client.get("/api/alerts/symbols")
        quote = client.get("/api/alerts/quotes", params={"symbols": "AAPL"})
    assert catalog.status_code == 200
    assert quote.status_code == 200
    for response in (catalog, quote):
        assert response.json()["prices"]["AAPL"] == 101
        assert response.json()["quote_meta"]["AAPL"] == {
            "source": "saved",
            "as_of": "2026-10-02T20:00:00+00:00",
            "retrieved_at": None,
        }


def test_hosted_database_saved_quote_selection(tmp_path, monkeypatch):
    database = tmp_path / "hosted.db"
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", f"sqlite:///{database.as_posix()}")
    from src.storage.database import init_database

    init_database()
    upsert_market_bars(
        [{"symbol": "AAPL", "close": 150, "quote_timestamp": "2026-10-02T15:07:00+00:00"}],
        "2026-10-02",
        data_dir=tmp_path,
    )
    upsert_market_bars(
        [{"symbol": "AAPL", "close": 151, "quote_timestamp": "2026-10-02T20:00:00+00:00"}],
        "2026-10-03",
        data_dir=tmp_path,
    )
    assert latest_saved_quotes(data_dir=tmp_path)["AAPL"] == {
        "price": 151.0,
        "as_of": "2026-10-02T20:00:00+00:00",
    }


def test_alert_health_reports_configuration_without_exposing_key(monkeypatch):
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    with patch("dashboard.backend.api.alerts._load_env"):
        missing = TestClient(app).get("/api/alerts/health")
        monkeypatch.setenv("FINNHUB_API_KEY", "secret-for-test")
        configured = TestClient(app).get("/api/alerts/health")
    assert missing.json()["live_quotes_configured"] is False
    assert configured.json()["live_quotes_configured"] is True
    assert "secret-for-test" not in configured.text


def test_lookup_time_is_not_misrepresented_as_market_quote_time(monkeypatch):
    monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)
    with (
        patch("dashboard.backend.api.alerts.resolve_symbol_prices", return_value={"AAPL": 150}),
        patch("dashboard.backend.api.alerts.saved_quote_details", return_value={}),
    ):
        response = TestClient(app).get("/api/alerts/quotes", params={"symbols": "AAPL"})
    assert response.status_code == 200
    meta = response.json()["quote_meta"]["AAPL"]
    assert meta["source"] == "lookup"
    assert meta["as_of"] is None
    assert datetime.fromisoformat(meta["retrieved_at"]).tzinfo is not None
