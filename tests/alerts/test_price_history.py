"""Close-history loader for RSI alert evaluation."""

from unittest.mock import MagicMock, call, patch

import pytest
import requests

from src.alerts.alert_rules import evaluate_rsi_threshold
from src.alerts.price_history import (
    closes_by_symbol,
    closes_from_candle_payload,
    load_symbol_closes,
    load_symbol_closes_from_market_bars,
    merge_latest_close,
)
from src.storage.database import init_database
from src.storage.market_bars import upsert_market_bars
from tests.helpers.market_bars import seed_daily_bars


def test_merge_latest_close_replaces_last_bar():
    assert merge_latest_close([10.0, 11.0], 12.0) == [10.0, 12.0]
    assert merge_latest_close([10.0, 11.0], 11.0) == [10.0, 11.0]
    assert merge_latest_close([], 9.5) == [9.5]


def test_merge_latest_close_ignores_non_finite_latest():
    series = [10.0, 11.0]
    assert merge_latest_close(series, float("nan")) == series
    assert merge_latest_close(series, float("inf")) == series
    assert merge_latest_close(series, "nope") == series
    assert merge_latest_close(series, None) == series


def test_closes_from_candle_payload_sorts_by_timestamp():
    payload = {
        "s": "ok",
        "t": [300, 100, 200],
        "c": [30.0, 10.0, 20.0],
    }
    assert closes_from_candle_payload(payload) == [10.0, 20.0, 30.0]
    assert closes_from_candle_payload({"s": "no_data", "c": [1.0]}) == []


def test_load_symbol_closes_prefers_provider_over_market_bars(tmp_path, monkeypatch):
    monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)
    seed_daily_bars(tmp_path, "2026-09-01", [{"symbol": "AAPL", "close": 1.0}])

    client = MagicMock()
    client.get_candle_data.return_value = {
        "s": "ok",
        "t": list(range(20)),
        "c": [float(i) for i in range(20)],
    }

    closes = load_symbol_closes("AAPL", data_dir=tmp_path, client=client)
    assert closes == [float(i) for i in range(20)]
    client.get_candle_data.assert_called_once()


def test_load_symbol_closes_constructs_default_finnhub_client(tmp_path, monkeypatch):
    """The production path must construct the real client without dependency injection."""
    monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)
    closes = [100.0 + i for i in range(20)]
    with patch("src.alerts.price_history.FinnhubClient") as client_class:
        client_class.return_value.get_candle_data.return_value = {"s": "ok", "c": closes}

        assert load_symbol_closes("AAPL", data_dir=tmp_path) == closes

    client_class.assert_called_once_with()
    client_class.return_value.get_candle_data.assert_called_once_with(
        "AAPL", resolution="D", days=90
    )


@pytest.mark.parametrize(
    "error",
    [
        ValueError("no key"),
        requests.exceptions.ConnectionError("provider unavailable"),
        requests.exceptions.Timeout("provider timed out"),
        requests.exceptions.HTTPError("rate limited"),
    ],
)
def test_load_symbol_closes_empty_when_provider_and_market_bars_are_empty(
    tmp_path, monkeypatch, error
):
    monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)
    client = MagicMock()
    client.get_candle_data.side_effect = error

    assert load_symbol_closes("AAPL", data_dir=tmp_path, client=client) == []


def test_load_symbol_closes_does_not_hide_unexpected_provider_errors(tmp_path, monkeypatch):
    monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)
    client = MagicMock()
    client.get_candle_data.side_effect = RuntimeError("unexpected provider bug")

    with pytest.raises(RuntimeError, match="unexpected provider bug"):
        load_symbol_closes("AAPL", data_dir=tmp_path, client=client)


def test_rsi_rule_degrades_gracefully_without_any_history(tmp_path, monkeypatch, caplog):
    """No provider candles and no market_bars: no crash, no trigger, clear log."""
    monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)
    client = MagicMock()
    client.get_candle_data.side_effect = ValueError("no key")

    with caplog.at_level("WARNING", logger="src.alerts.price_history"):
        history = closes_by_symbol(["AAPL"], [], data_dir=tmp_path, client=client)

    assert history == {"AAPL": []}
    assert "No close history for AAPL" in caplog.text
    condition = {"type": "rsi_threshold", "symbol": "AAPL", "operator": "less_than", "value": 30}
    assert evaluate_rsi_threshold(condition, history["AAPL"]) is False


def test_load_symbol_closes_ignores_leftover_daily_csv(tmp_path, monkeypatch):
    """Legacy daily_data CSVs are no longer a price-history source."""
    monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)
    for day, close in [("2026-09-01", 100.0), ("2026-09-02", 101.0)]:
        (tmp_path / f"daily_data_{day}.csv").write_text(
            f"symbol,close\nAAPL,{close}\n",
            encoding="utf-8",
        )
    client = MagicMock()
    client.get_candle_data.side_effect = ValueError("no key")

    assert load_symbol_closes("AAPL", data_dir=tmp_path, client=client) == []


def test_load_symbol_closes_from_market_bars_sidecar(tmp_path, monkeypatch):
    monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)
    seed_daily_bars(tmp_path, "2026-09-01", [{"symbol": "AAPL", "close": 100.0}])
    seed_daily_bars(tmp_path, "2026-09-02", [{"symbol": "AAPL", "close": 101.5}])
    seed_daily_bars(tmp_path, "2026-09-03", [{"symbol": "AAPL", "close": 99.0}])

    assert load_symbol_closes_from_market_bars("AAPL", data_dir=tmp_path) == [
        100.0,
        101.5,
        99.0,
    ]
    assert load_symbol_closes_from_market_bars("msft", data_dir=tmp_path) == []


def test_load_symbol_closes_uses_market_bars_when_csv_gone(tmp_path, monkeypatch):
    """Post-#635: no daily_data CSV is written; RSI must still see saved bars."""
    monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)
    seed_daily_bars(tmp_path, "2026-09-01", [{"symbol": "AAPL", "close": 100.0}])
    seed_daily_bars(tmp_path, "2026-09-02", [{"symbol": "AAPL", "close": 101.0}])
    client = MagicMock()
    client.get_candle_data.side_effect = requests.exceptions.HTTPError("rate limited")

    closes = load_symbol_closes("AAPL", data_dir=tmp_path, client=client)
    assert closes == [100.0, 101.0]
    assert list(tmp_path.glob("daily_data_*.csv")) == []


def test_load_symbol_closes_from_hosted_market_bars(tmp_path, monkeypatch):
    db_path = tmp_path / "hosted.db"
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    init_database()
    upsert_market_bars(
        [{"symbol": "AAPL", "close": 200.0}],
        "2026-09-10",
        source="test",
    )
    upsert_market_bars(
        [{"symbol": "AAPL", "close": 202.0}],
        "2026-09-11",
        source="test",
    )
    client = MagicMock()
    client.get_candle_data.side_effect = ValueError("no key")

    # data_dir is ignored in hosted mode; bars come from the app database.
    closes = load_symbol_closes("AAPL", data_dir=tmp_path / "unused", client=client)
    assert closes == [200.0, 202.0]


def test_load_symbol_closes_prefers_local_when_provider_is_partial(tmp_path, monkeypatch):
    """Short Finnhub series must not beat a longer durable market_bars history."""
    monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)
    local = [100.0 + i for i in range(12)]
    for i, close in enumerate(local):
        seed_daily_bars(
            tmp_path,
            f"2026-09-{i + 1:02d}",
            [{"symbol": "AAPL", "close": close}],
        )
    client = MagicMock()
    client.get_candle_data.return_value = {
        "s": "ok",
        "c": [float(i) for i in range(10)],
    }

    closes = load_symbol_closes("AAPL", data_dir=tmp_path, client=client)
    assert closes == local


def test_load_symbol_closes_keeps_partial_provider_when_longer_than_local(tmp_path, monkeypatch):
    monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)
    seed_daily_bars(tmp_path, "2026-09-01", [{"symbol": "AAPL", "close": 1.0}])
    seed_daily_bars(tmp_path, "2026-09-02", [{"symbol": "AAPL", "close": 2.0}])
    client = MagicMock()
    client.get_candle_data.return_value = {
        "s": "ok",
        "c": [10.0, 11.0, 12.0, 13.0],
    }

    closes = load_symbol_closes("AAPL", data_dir=tmp_path, client=client)
    assert closes == [10.0, 11.0, 12.0, 13.0]


def test_closes_by_symbol_overlays_snapshot_and_skips_bad_latest():
    with patch(
        "src.alerts.price_history.load_symbol_closes",
        return_value=[100.0, 101.0],
    ) as loader:
        overlaid = closes_by_symbol(
            ["AAPL", "aapl"],
            [{"symbol": "AAPL", "close": 105.0}],
            prefer_provider=False,
        )
        ignored = closes_by_symbol(
            ["AAPL"],
            [{"symbol": "AAPL", "close": float("nan")}],
            prefer_provider=False,
        )

    assert overlaid == {"AAPL": [100.0, 105.0]}
    assert ignored == {"AAPL": [100.0, 101.0]}
    assert loader.call_count == 2


def test_closes_by_symbol_shares_default_client_across_unique_symbols():
    closes = [100.0 + i for i in range(20)]
    with patch("src.alerts.price_history.FinnhubClient") as client_class:
        client_class.return_value.get_candle_data.return_value = {"s": "ok", "c": closes}

        history = closes_by_symbol(["AAPL", "msft", "aapl"], [])

    assert history == {"AAPL": closes, "MSFT": closes}
    client_class.assert_called_once_with()
    assert client_class.return_value.get_candle_data.call_args_list == [
        call("AAPL", resolution="D", days=90),
        call("MSFT", resolution="D", days=90),
    ]


@pytest.mark.parametrize("symbols,prefer_provider", [([], True), ([""], True), (["AAPL"], False)])
def test_closes_by_symbol_does_not_construct_unused_client(symbols, prefer_provider):
    with (
        patch("src.alerts.price_history.FinnhubClient") as client_class,
        patch("src.alerts.price_history.load_symbol_closes_from_local", return_value=[]),
    ):
        closes_by_symbol(symbols, [], prefer_provider=prefer_provider)

    client_class.assert_not_called()


def test_closes_by_symbol_uses_saved_bars_when_default_client_cannot_start(tmp_path, monkeypatch):
    monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)
    seed_daily_bars(
        tmp_path,
        "2026-09-01",
        [{"symbol": "AAPL", "close": 100.0}, {"symbol": "MSFT", "close": 200.0}],
    )
    with patch(
        "src.alerts.price_history.FinnhubClient", side_effect=ValueError("no key")
    ) as client_class:
        history = closes_by_symbol(["AAPL", "MSFT"], [], data_dir=tmp_path)

    assert history == {"AAPL": [100.0], "MSFT": [200.0]}
    client_class.assert_called_once_with()
