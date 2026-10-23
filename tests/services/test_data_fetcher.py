"""Tests for StockDataFetcher retry and index aggregation edges."""

from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.services.data_fetcher import StockDataFetcher


def test_stock_data_fetcher_requires_api_client(monkeypatch):
    monkeypatch.setattr(
        "src.services.data_fetcher.FinnhubClient",
        MagicMock(side_effect=ValueError("API key required")),
    )

    with pytest.raises(ValueError, match="API key required"):
        StockDataFetcher()


def test_fetch_symbol_data_returns_payload_on_success():
    client = MagicMock()
    client.get_stock_data.return_value = {"symbol": "AAPL", "close": 190.0}
    fetcher = StockDataFetcher(api_client=client, include_profile=False)

    assert fetcher.fetch_symbol_data("AAPL") == {"symbol": "AAPL", "close": 190.0}
    client.get_stock_data.assert_called_once_with("AAPL", include_profile=False)


def test_fetch_symbol_data_retries_then_returns_none(monkeypatch):
    client = MagicMock()
    client.get_stock_data.side_effect = RuntimeError("rate limited")
    fetcher = StockDataFetcher(api_client=client, include_profile=False)
    sleeps: list[float] = []
    monkeypatch.setattr("src.services.data_fetcher.time.sleep", sleeps.append)

    assert fetcher.fetch_symbol_data("NVDA", max_retries=3) is None
    assert client.get_stock_data.call_count == 3
    assert sleeps == [0.5, 1.0]


@patch("src.services.data_fetcher.get_indices_to_track", return_value=["S&P 500"])
@patch("src.services.data_fetcher.IndexFetcher")
def test_fetch_all_indices_caps_symbols_and_skips_empty(
    mock_index_fetcher_cls, _mock_indices, monkeypatch
):
    index_fetcher = MagicMock()
    index_fetcher.get_index_symbols.return_value = [f"S{i}" for i in range(150)]
    mock_index_fetcher_cls.return_value = index_fetcher

    client = MagicMock()
    fetcher = StockDataFetcher(api_client=client, include_profile=False)

    def fake_fetch(symbol: str, **_kwargs):
        if symbol == "S0":
            return {"symbol": symbol, "close": 1.0}
        return None

    monkeypatch.setattr(fetcher, "fetch_symbol_data", fake_fetch)
    monkeypatch.setenv("STOCK_FETCH_MAX_WORKERS", "1")
    monkeypatch.setattr("src.services.data_fetcher.time.sleep", lambda _seconds: None)

    result = fetcher.fetch_all_indices(use_screener=False)

    assert list(result.keys()) == ["S&P 500"]
    assert result["S&P 500"] == [{"symbol": "S0", "close": 1.0, "index_name": "S&P 500"}]


@patch("src.services.data_fetcher.get_indices_to_track", return_value=["NASDAQ-100"])
@patch("src.services.data_fetcher.IndexFetcher")
def test_fetch_all_indices_continues_when_index_has_no_symbols(
    mock_index_fetcher_cls, _mock_indices
):
    index_fetcher = MagicMock()
    index_fetcher.get_index_symbols.return_value = []
    mock_index_fetcher_cls.return_value = index_fetcher
    fetcher = StockDataFetcher(api_client=MagicMock(), include_profile=False)

    assert fetcher.fetch_all_indices(use_screener=False) == {}


@patch("src.services.data_fetcher.get_indices_to_track", return_value=["S&P 500"])
@patch("src.services.data_fetcher.IndexFetcher")
def test_fetch_all_indices_uses_default_filters_when_json_corrupt(
    mock_index_fetcher_cls, _mock_indices, monkeypatch
):
    """Corrupt filters.json must soft-fail to StockScreener defaults (None)."""

    symbols = [f"S{i}" for i in range(25)]
    index_fetcher = MagicMock()
    index_fetcher.get_index_symbols.return_value = symbols
    mock_index_fetcher_cls.return_value = index_fetcher

    filters_suffix = str(Path("config") / "filters.json")
    real_exists = Path.exists
    real_open = open

    def exists_with_filters(self):
        if str(self).endswith(filters_suffix) or self.name == "filters.json":
            return True
        return real_exists(self)

    def open_corrupt_filters(file, *args, **kwargs):
        path_str = str(file)
        if path_str.endswith(filters_suffix) or path_str.endswith("filters.json"):
            return StringIO("{not-valid-json")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(Path, "exists", exists_with_filters)
    monkeypatch.setattr("builtins.open", open_corrupt_filters)

    screener = MagicMock()
    screener.get_qualified_symbols.return_value = ["S0", "S1"]
    screener_cls = MagicMock(return_value=screener)

    fetcher = StockDataFetcher(api_client=MagicMock(), include_profile=False)
    monkeypatch.setattr(
        fetcher, "fetch_symbol_data", lambda symbol, **_k: {"symbol": symbol, "close": 1.0}
    )
    monkeypatch.setenv("STOCK_FETCH_MAX_WORKERS", "1")
    monkeypatch.setattr("src.services.data_fetcher.time.sleep", lambda _seconds: None)

    with patch("src.services.stock_screener.StockScreener", screener_cls):
        result = fetcher.fetch_all_indices(use_screener=True)

    screener_cls.assert_called_once()
    assert screener_cls.call_args.args[0] is None
    assert sorted(row["symbol"] for row in result["S&P 500"]) == ["S0", "S1"]


@patch("src.services.data_fetcher.get_indices_to_track", return_value=["S&P 500"])
@patch("src.services.data_fetcher.IndexFetcher")
def test_fetch_all_indices_continues_when_symbol_fetch_raises(
    mock_index_fetcher_cls, _mock_indices, monkeypatch
):
    """One symbol future exception must not abort the rest of the index batch."""
    index_fetcher = MagicMock()
    index_fetcher.get_index_symbols.return_value = ["GOOD", "BAD", "OK"]
    mock_index_fetcher_cls.return_value = index_fetcher

    fetcher = StockDataFetcher(api_client=MagicMock(), include_profile=False)

    def fake_fetch(symbol: str, **_kwargs):
        if symbol == "BAD":
            raise RuntimeError("thread boom")
        return {"symbol": symbol, "close": 10.0}

    monkeypatch.setattr(fetcher, "fetch_symbol_data", fake_fetch)
    monkeypatch.setenv("STOCK_FETCH_MAX_WORKERS", "1")
    monkeypatch.setattr("src.services.data_fetcher.time.sleep", lambda _seconds: None)

    result = fetcher.fetch_all_indices(use_screener=False)

    symbols = sorted(row["symbol"] for row in result["S&P 500"])
    assert symbols == ["GOOD", "OK"]
    assert all(row["index_name"] == "S&P 500" for row in result["S&P 500"])


@patch("src.services.data_fetcher.get_indices_to_track", return_value=["S&P 500"])
@patch("src.services.data_fetcher.IndexFetcher")
@patch("src.services.data_fetcher.ThreadPoolExecutor")
def test_fetch_all_indices_clamps_invalid_and_oversized_worker_env(
    mock_executor_cls, mock_index_fetcher_cls, _mock_indices, monkeypatch
):
    index_fetcher = MagicMock()
    index_fetcher.get_index_symbols.return_value = ["AAPL"]
    mock_index_fetcher_cls.return_value = index_fetcher

    executor = MagicMock()
    executor.__enter__.return_value = executor
    executor.__exit__.return_value = False
    executor.submit.side_effect = lambda fn, *args, **kwargs: MagicMock()
    # as_completed yields nothing → empty results; we only care about max_workers
    mock_executor_cls.return_value = executor

    fetcher = StockDataFetcher(api_client=MagicMock(), include_profile=False)
    monkeypatch.setattr("src.services.data_fetcher.as_completed", lambda _futs: [])
    monkeypatch.setattr("src.services.data_fetcher.time.sleep", lambda _seconds: None)

    monkeypatch.setenv("STOCK_FETCH_MAX_WORKERS", "not-an-int")
    fetcher.fetch_all_indices(use_screener=False)
    assert mock_executor_cls.call_args.kwargs["max_workers"] == 2

    monkeypatch.setenv("STOCK_FETCH_MAX_WORKERS", "99")
    fetcher.fetch_all_indices(use_screener=False)
    assert mock_executor_cls.call_args.kwargs["max_workers"] == 4

    monkeypatch.setenv("STOCK_FETCH_MAX_WORKERS", "0")
    fetcher.fetch_all_indices(use_screener=False)
    assert mock_executor_cls.call_args.kwargs["max_workers"] == 1


@patch("src.services.data_fetcher.get_indices_to_track", return_value=["S&P 500"])
@patch("src.services.data_fetcher.IndexFetcher")
def test_fetch_all_indices_respects_max_symbols_per_index(
    mock_index_fetcher_cls, _mock_indices, monkeypatch
):
    index_fetcher = MagicMock()
    index_fetcher.get_index_symbols.return_value = [f"S{i}" for i in range(50)]
    mock_index_fetcher_cls.return_value = index_fetcher

    fetcher = StockDataFetcher(api_client=MagicMock(), include_profile=False)
    fetched: list[str] = []

    def fake_fetch(symbol: str, **_kwargs):
        fetched.append(symbol)
        return {"symbol": symbol, "close": 1.0}

    monkeypatch.setattr(fetcher, "fetch_symbol_data", fake_fetch)
    monkeypatch.setenv("STOCK_FETCH_MAX_WORKERS", "1")
    monkeypatch.setattr("src.services.data_fetcher.time.sleep", lambda _seconds: None)

    result = fetcher.fetch_all_indices(use_screener=False, max_symbols_per_index=5)

    assert fetched == ["S0", "S1", "S2", "S3", "S4"]
    assert len(result["S&P 500"]) == 5


@patch("src.services.data_fetcher.get_indices_to_track", return_value=["S&P 500"])
@patch("src.services.data_fetcher.IndexFetcher")
@patch("src.services.stock_screener.StockScreener")
def test_fetch_all_indices_screens_when_more_than_20_symbols(
    mock_screener_cls, mock_index_fetcher_cls, _mock_indices, monkeypatch
):
    symbols = [f"S{i}" for i in range(25)]
    index_fetcher = MagicMock()
    index_fetcher.get_index_symbols.return_value = symbols
    mock_index_fetcher_cls.return_value = index_fetcher

    screener = MagicMock()
    screener.get_qualified_symbols.return_value = ["S0", "S1"]
    mock_screener_cls.return_value = screener

    client = MagicMock()
    fetcher = StockDataFetcher(api_client=client, include_profile=False)
    monkeypatch.setattr(
        fetcher,
        "fetch_symbol_data",
        lambda symbol, **_kwargs: {"symbol": symbol, "close": 10.0},
    )
    monkeypatch.setenv("STOCK_FETCH_MAX_WORKERS", "1")
    monkeypatch.setattr("src.services.data_fetcher.time.sleep", lambda _seconds: None)

    result = fetcher.fetch_all_indices(use_screener=True)

    mock_screener_cls.assert_called_once_with(None, api_client=client)
    screener.get_qualified_symbols.assert_called_once_with(symbols, max_workers=2)
    assert [row["symbol"] for row in result["S&P 500"]] == ["S0", "S1"]
    assert all(row["index_name"] == "S&P 500" for row in result["S&P 500"])


@patch("src.services.data_fetcher.get_indices_to_track", return_value=["S&P 500"])
@patch("src.services.data_fetcher.IndexFetcher")
@patch("src.services.stock_screener.StockScreener")
def test_fetch_all_indices_falls_back_when_screener_raises(
    mock_screener_cls, mock_index_fetcher_cls, _mock_indices, monkeypatch
):
    """Screener boom must not abort the index; keep capped unscreened symbols."""
    symbols = [f"S{i}" for i in range(25)]
    index_fetcher = MagicMock()
    index_fetcher.get_index_symbols.return_value = symbols
    mock_index_fetcher_cls.return_value = index_fetcher

    screener = MagicMock()
    screener.get_qualified_symbols.side_effect = RuntimeError("screener API down")
    mock_screener_cls.return_value = screener

    fetched: list[str] = []
    fetcher = StockDataFetcher(api_client=MagicMock(), include_profile=False)

    def fake_fetch(symbol: str, **_kwargs):
        fetched.append(symbol)
        return {"symbol": symbol, "close": 1.0}

    monkeypatch.setattr(fetcher, "fetch_symbol_data", fake_fetch)
    monkeypatch.setenv("STOCK_FETCH_MAX_WORKERS", "1")
    monkeypatch.setattr("src.services.data_fetcher.time.sleep", lambda _seconds: None)

    result = fetcher.fetch_all_indices(use_screener=True, max_symbols_per_index=5)

    assert fetched == ["S0", "S1", "S2", "S3", "S4"]
    assert [row["symbol"] for row in result["S&P 500"]] == fetched


@patch("src.services.data_fetcher.get_indices_to_track", return_value=["S&P 500"])
@patch("src.services.data_fetcher.IndexFetcher")
@patch("src.services.stock_screener.StockScreener")
def test_fetch_all_indices_skips_screener_when_20_or_fewer_symbols(
    mock_screener_cls, mock_index_fetcher_cls, _mock_indices, monkeypatch
):
    symbols = [f"S{i}" for i in range(20)]
    index_fetcher = MagicMock()
    index_fetcher.get_index_symbols.return_value = symbols
    mock_index_fetcher_cls.return_value = index_fetcher

    client = MagicMock()
    fetcher = StockDataFetcher(api_client=client, include_profile=False)
    fetched: list[str] = []

    def fake_fetch(symbol: str, **_kwargs):
        fetched.append(symbol)
        return {"symbol": symbol, "close": 1.0}

    monkeypatch.setattr(fetcher, "fetch_symbol_data", fake_fetch)
    monkeypatch.setenv("STOCK_FETCH_MAX_WORKERS", "1")
    monkeypatch.setattr("src.services.data_fetcher.time.sleep", lambda _seconds: None)

    result = fetcher.fetch_all_indices(use_screener=True)

    mock_screener_cls.assert_not_called()
    assert sorted(fetched) == sorted(symbols)
    assert len(result["S&P 500"]) == 20
