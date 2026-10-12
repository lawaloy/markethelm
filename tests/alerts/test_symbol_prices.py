"""Tests for symbol price resolution."""

from unittest.mock import MagicMock, patch

from src.alerts.symbol_prices import (
    prices_from_saved_daily_data,
    resolve_symbol_prices,
    saved_quote_details,
)
from src.storage.market_bars import latest_saved_quotes, upsert_market_bars


@patch("dashboard.backend.services.data_loader.get_data_loader")
def test_prices_from_saved_daily_data(mock_get_loader, tmp_path):
    mock_get_loader.return_value.data_dir = tmp_path
    upsert_market_bars(
        [{"symbol": "AAPL", "close": 180.5}, {"symbol": "GOOGL", "close": 170.25}],
        "2026-10-02",
        data_dir=tmp_path,
    )
    assert prices_from_saved_daily_data() == {"AAPL": 180.5, "GOOGL": 170.25}


@patch("dashboard.backend.services.data_loader.get_data_loader")
def test_saved_quote_details_ignores_mock_data_directory(mock_get_loader):
    assert saved_quote_details() == {}
    assert isinstance(mock_get_loader.return_value.data_dir, MagicMock)


@patch("src.services.data_fetcher.StockDataFetcher")
@patch("src.alerts.symbol_prices.prices_from_saved_daily_data", return_value={"AAPL": 180.5})
def test_resolve_symbol_prices_fetches_missing(_mock_saved, mock_fetcher_cls):
    fetcher = MagicMock()
    fetcher.fetch_symbol_data.return_value = {"symbol": "NVDA", "close": 900.0}
    mock_fetcher_cls.return_value = fetcher

    prices = resolve_symbol_prices(["AAPL", "NVDA"], fetch_missing=True)

    assert prices == {"AAPL": 180.5, "NVDA": 900.0}
    fetcher.fetch_symbol_data.assert_called_once_with("NVDA")


@patch(
    "src.alerts.symbol_prices.prices_from_saved_daily_data",
    return_value={"AAPL": 180.5, "MSFT": 400.0},
)
def test_resolve_symbol_prices_skips_blank_and_dedupes_case(_mock_saved):
    """Quote pickers must ignore empty tokens and treat ticker case as identical."""
    prices = resolve_symbol_prices(
        ["", "  ", "aapl", "AAPL", "msft"],
        fetch_missing=False,
    )

    assert prices == {"AAPL": 180.5, "MSFT": 400.0}


@patch(
    "src.alerts.symbol_prices.prices_from_saved_daily_data",
    return_value={"AAPL": 180.5, "MSFT": 400.0},
)
def test_resolve_symbol_prices_strips_surrounding_whitespace(_mock_saved):
    """Surrounding whitespace must not prevent matching saved tickers."""
    prices = resolve_symbol_prices(
        [" AAPL ", "\tMSFT\n", "  "],
        fetch_missing=False,
    )

    assert prices == {"AAPL": 180.5, "MSFT": 400.0}


def test_resolve_symbol_prices_returns_empty_for_blank_only_input():
    assert resolve_symbol_prices(["", "   "], fetch_missing=False) == {}


@patch("dashboard.backend.services.data_loader.get_data_loader")
def test_prices_from_saved_daily_data_skips_invalid_symbol_tokens(mock_get_loader, tmp_path):
    """None/NaN/blank symbols must not enter the quote map as NONE/NAN."""
    upsert_market_bars(
        [
            {"symbol": "AAPL", "close": 180.5},
            {"symbol": None, "close": 1.0},
            {"symbol": float("nan"), "close": 2.0},
            {"symbol": "  ", "close": 3.0},
            {"symbol": " msft ", "close": 400.0},
        ],
        "2026-10-02",
        data_dir=tmp_path,
    )
    mock_get_loader.return_value.data_dir = tmp_path
    assert prices_from_saved_daily_data() == {"AAPL": 180.5, "MSFT": 400.0}


@patch("dashboard.backend.services.data_loader.get_data_loader")
def test_prices_from_saved_daily_data_skips_invalid_rows_and_loader_errors(
    mock_get_loader, tmp_path
):
    upsert_market_bars(
        [
            {"symbol": "AAPL", "close": 180.5},
            {"symbol": "", "close": 12.0},
            {"symbol": "BAD", "close": "n/a"},
        ],
        "2026-10-02",
        data_dir=tmp_path,
    )
    mock_get_loader.return_value.data_dir = tmp_path
    assert prices_from_saved_daily_data() == {"AAPL": 180.5}

    next_dir = tmp_path / "next"
    upsert_market_bars(
        [{"symbol": "GOOG", "price": 140.0}],
        "2026-10-02",
        data_dir=next_dir,
    )
    mock_get_loader.return_value.data_dir = next_dir
    assert prices_from_saved_daily_data() == {"GOOG": 140.0}

    mock_get_loader.side_effect = ValueError("No daily data files found")
    assert prices_from_saved_daily_data() == {}


@patch("dashboard.backend.services.data_loader.get_data_loader")
def test_prices_from_saved_daily_data_soft_fails_loader_oserror(mock_get_loader):
    """Unreadable data dirs must not crash alert quote pickers."""
    mock_get_loader.side_effect = OSError("permission denied")
    assert prices_from_saved_daily_data() == {}


@patch("dashboard.backend.services.data_loader.get_data_loader")
def test_prices_from_saved_daily_data_soft_fails_loader_runtime_error(mock_get_loader):
    """Loader boot / runtime failures must degrade to empty saved prices."""
    mock_get_loader.side_effect = RuntimeError("data loader unavailable")
    assert prices_from_saved_daily_data() == {}


@patch("dashboard.backend.services.data_loader.get_data_loader")
def test_prices_from_saved_daily_data_keeps_zero_close(mock_get_loader, tmp_path):
    """A $0 saved close must still seed the picker; `if not close` would refetch."""
    upsert_market_bars(
        [
            {"symbol": "AAPL", "close": 0},
            {"symbol": "MSFT", "close": 400.0},
        ],
        "2026-10-02",
        data_dir=tmp_path,
    )
    mock_get_loader.return_value.data_dir = tmp_path
    assert prices_from_saved_daily_data() == {"AAPL": 0.0, "MSFT": 400.0}


@patch("src.services.data_fetcher.StockDataFetcher")
@patch(
    "src.alerts.symbol_prices.prices_from_saved_daily_data",
    return_value={"AAPL": 0.0},
)
def test_resolve_symbol_prices_does_not_fetch_when_saved_close_is_zero(
    _mock_saved, mock_fetcher_cls
):
    """Zero is present, not missing — do not burn Finnhub to "fill" it."""
    prices = resolve_symbol_prices(["AAPL"], fetch_missing=True)
    assert prices == {"AAPL": 0.0}
    mock_fetcher_cls.assert_not_called()


@patch("src.services.data_fetcher.StockDataFetcher")
@patch("src.alerts.symbol_prices.prices_from_saved_daily_data", return_value={})
def test_resolve_symbol_prices_keeps_zero_live_quote(_mock_saved, mock_fetcher_cls):
    fetcher = MagicMock()
    fetcher.fetch_symbol_data.return_value = {"symbol": "MSFT", "close": 0}
    mock_fetcher_cls.return_value = fetcher

    prices = resolve_symbol_prices(["MSFT"], fetch_missing=True)

    assert prices == {"MSFT": 0.0}
    fetcher.fetch_symbol_data.assert_called_once_with("MSFT")


@patch("dashboard.backend.services.data_loader.get_data_loader")
def test_prices_from_saved_daily_data_skips_non_finite_closes(mock_get_loader, tmp_path):
    """NaN/inf closes must not enter the quote map as JSON-null floats."""
    upsert_market_bars(
        [
            {"symbol": "AAPL", "close": 180.5},
            {"symbol": "NAN", "close": float("nan")},
            {"symbol": "INF", "close": float("inf")},
            {"symbol": "NINF", "close": float("-inf")},
        ],
        "2026-10-02",
        data_dir=tmp_path,
    )
    mock_get_loader.return_value.data_dir = tmp_path
    assert prices_from_saved_daily_data() == {"AAPL": 180.5}


def test_latest_saved_quote_uses_provider_time_across_weekend_snapshots(tmp_path):
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
    assert latest_saved_quotes(data_dir=tmp_path)["AAPL"] == {
        "price": 101.0,
        "as_of": "2026-10-02T20:00:00+00:00",
    }


def test_latest_saved_quote_prefers_provider_time_over_newer_storage_date(tmp_path):
    """A later snapshot without a provider timestamp must not beat an older quote."""
    upsert_market_bars(
        [{"symbol": "AAPL", "close": 100, "quote_timestamp": "2026-10-02T20:00:00+00:00"}],
        "2026-10-02",
        data_dir=tmp_path,
    )
    upsert_market_bars(
        [{"symbol": "AAPL", "close": 99}],
        "2026-10-03",
        data_dir=tmp_path,
    )
    assert latest_saved_quotes(data_dir=tmp_path)["AAPL"] == {
        "price": 100.0,
        "as_of": "2026-10-02T20:00:00+00:00",
    }


def test_latest_saved_quote_invalid_provider_time_falls_back_to_trade_date(tmp_path):
    upsert_market_bars(
        [{"symbol": "AAPL", "close": 100, "quote_timestamp": "not-iso"}],
        "2026-10-02",
        data_dir=tmp_path,
    )
    assert latest_saved_quotes(data_dir=tmp_path)["AAPL"] == {
        "price": 100.0,
        "as_of": "2026-10-02",
    }


@patch("src.services.data_fetcher.StockDataFetcher")
@patch("src.alerts.symbol_prices.prices_from_saved_daily_data", return_value={})
def test_resolve_symbol_prices_skips_non_finite_live_quotes(_mock_saved, mock_fetcher_cls):
    """Live Finnhub quotes with NaN/inf closes must not enter the quote map."""
    fetcher = MagicMock()

    def fetch_symbol_data(symbol: str):
        if symbol == "OK":
            return {"symbol": symbol, "close": 12.5}
        if symbol == "NAN":
            return {"symbol": symbol, "close": float("nan")}
        if symbol == "INF":
            return {"symbol": symbol, "price": float("inf")}
        return {"symbol": symbol, "close": float("-inf")}

    fetcher.fetch_symbol_data.side_effect = fetch_symbol_data
    mock_fetcher_cls.return_value = fetcher

    prices = resolve_symbol_prices(["OK", "NAN", "INF", "NINF"], fetch_missing=True)
    assert prices == {"OK": 12.5}


@patch("src.services.data_fetcher.StockDataFetcher", side_effect=ValueError("API key required"))
@patch(
    "src.alerts.symbol_prices.prices_from_saved_daily_data",
    return_value={"AAPL": 180.5},
)
def test_resolve_symbol_prices_keeps_saved_when_live_fetcher_unavailable(
    _mock_saved, _mock_fetcher_cls
):
    prices = resolve_symbol_prices(["AAPL", "NVDA"], fetch_missing=True)
    assert prices == {"AAPL": 180.5}


@patch("src.services.data_fetcher.StockDataFetcher")
@patch("src.alerts.symbol_prices.prices_from_saved_daily_data", return_value={})
def test_resolve_symbol_prices_duplicates_do_not_starve_live_cap(_mock_saved, mock_fetcher_cls):
    """Padded duplicates must unique before the 15-symbol live-fetch budget.

    Settings quote pickers can pass the same ticker many times. If the cap
    sliced the raw list first, AAPL copies would fill the window and unique
    symbols would never reach Finnhub.
    """
    fetcher = MagicMock()
    fetcher.fetch_symbol_data.side_effect = lambda symbol: {
        "symbol": symbol,
        "close": 1.0,
    }
    mock_fetcher_cls.return_value = fetcher

    duplicates = [" aapl ", "AAPL", "AAPL"] * 7
    unique_rest = [f"S{i:02d}" for i in range(14)]
    prices = resolve_symbol_prices(duplicates + unique_rest, fetch_missing=True)

    expected = ["AAPL", *unique_rest]
    assert fetcher.fetch_symbol_data.call_count == 15
    assert {c.args[0] for c in fetcher.fetch_symbol_data.call_args_list} == set(expected)
    assert prices == {symbol: 1.0 for symbol in expected}
    mock_fetcher_cls.assert_called_once_with(include_profile=False)


@patch("src.services.data_fetcher.StockDataFetcher")
@patch("src.alerts.symbol_prices.prices_from_saved_daily_data", return_value={})
def test_resolve_symbol_prices_caps_live_fetches_and_skips_bad_quotes(
    _mock_saved, mock_fetcher_cls
):
    fetcher = MagicMock()

    def fetch_symbol_data(symbol: str):
        if symbol == "OK1":
            return {"symbol": symbol, "close": 10.0}
        if symbol == "BAD":
            raise RuntimeError("quote failed")
        if symbol == "EMPTY":
            return None
        if symbol == "NAN":
            return {"symbol": symbol, "close": "x"}
        return {"symbol": symbol, "close": 1.0}

    fetcher.fetch_symbol_data.side_effect = fetch_symbol_data
    mock_fetcher_cls.return_value = fetcher

    # 16 missing symbols: only the first 15 should be submitted for live fetch.
    symbols = [f"S{i}" for i in range(13)] + ["OK1", "BAD", "EMPTY", "NAN"]
    prices = resolve_symbol_prices(symbols, fetch_missing=True)

    assert prices == {"OK1": 10.0, **{f"S{i}": 1.0 for i in range(13)}}
    assert fetcher.fetch_symbol_data.call_count == 15
    fetched = {call.args[0] for call in fetcher.fetch_symbol_data.call_args_list}
    assert fetched == {f"S{i}" for i in range(13)} | {"OK1", "BAD"}
    assert "EMPTY" not in fetched
    assert "NAN" not in fetched
