"""Tests for history symbol catalog builders."""

from unittest.mock import MagicMock, patch

import pandas as pd

from dashboard.backend.api.history import (
    _load_index_symbol_names,
    _resolve_company_names,
    build_symbol_catalog,
    load_index_symbol_names,
)


@patch("dashboard.backend.api.history.load_index_symbol_names", return_value={"AAPL": "Apple Inc."})
@patch("dashboard.backend.api.history.get_data_loader")
def test_build_symbol_catalog_skips_none_nan_blank_projection_symbols(
    mock_get_loader, _mock_index_names
):
    """Corrupt projection symbols must not leak as NONE/NAN/blank pickers."""
    loader = MagicMock()
    loader.load_projections.return_value = pd.DataFrame(
        {
            "symbol": ["AAPL", None, float("nan"), "", "  ", "msft", "GOOG"],
            "name": [
                "Apple Inc.",
                "Nope",
                "Nope",
                "Nope",
                "Nope",
                "Microsoft",
                "Alphabet",
            ],
        }
    )
    mock_get_loader.return_value = loader

    symbols, names = build_symbol_catalog()

    assert "NONE" not in symbols
    assert "NAN" not in symbols
    assert "" not in symbols
    assert "AAPL" in symbols
    assert "MSFT" in symbols
    assert "GOOG" in symbols
    assert names["MSFT"] == "Microsoft"
    assert names["GOOG"] == "Alphabet"


def test_resolve_company_names_skips_sentinels_and_strips_padding():
    """Name map keys must be normalized; None/NaN must not become NONE/NAN."""
    with patch(
        "dashboard.backend.api.history.load_index_symbol_names",
        return_value={"AAPL": "Apple Inc.", "MSFT": "Microsoft"},
    ):
        names = _resolve_company_names([" aapl ", None, float("nan"), "", "msft", "NONE"])

    assert names == {"AAPL": "Apple Inc.", "MSFT": "Microsoft"}
    assert "NONE" not in names
    assert "NAN" not in names


def test_load_index_symbol_names_normalizes_and_skips_bad_symbols():
    """Index catalog must strip padding and drop blank/sentinel symbols."""

    class _FakeStock:
        def __init__(self, symbol, name):
            self._symbol = symbol
            self._name = name

        def get(self, key, default=None):
            if key == "symbol":
                return self._symbol
            if key == "name":
                return self._name
            return default

    class _FakePyTickerSymbols:
        def get_stocks_by_index(self, index_name):
            if index_name == "S&P 500":
                return [
                    _FakeStock(" aapl ", "Apple Inc."),
                    _FakeStock(None, "Nope"),
                    _FakeStock(float("nan"), "Nope"),
                    _FakeStock("msft", "Microsoft"),
                ]
            return []

    fake_module = MagicMock()
    fake_module.PyTickerSymbols = _FakePyTickerSymbols

    with patch("dashboard.backend.api.history.PyTickerSymbols", fake_module.PyTickerSymbols):
        names = load_index_symbol_names()

    assert names == {"AAPL": "Apple Inc.", "MSFT": "Microsoft"}
    assert "NONE" not in names
    assert "NAN" not in names


def test_load_index_symbol_names_reuses_static_provider_catalog():
    """Repeated API calls must not rebuild the 500-company index catalog."""
    _load_index_symbol_names.cache_clear()
    provider_calls = 0

    class _FakePyTickerSymbols:
        def __init__(self):
            nonlocal provider_calls
            provider_calls += 1

        def get_stocks_by_index(self, index_name):
            if index_name == "S&P 500":
                return [{"symbol": "AAPL", "name": "Apple Inc."}]
            return []

    fake_module = MagicMock()
    fake_module.PyTickerSymbols = _FakePyTickerSymbols

    with patch("dashboard.backend.api.history.PyTickerSymbols", fake_module.PyTickerSymbols):
        first = load_index_symbol_names()
        first["AAPL"] = "mutated"
        second = load_index_symbol_names()

    assert provider_calls == 1
    assert second == {"AAPL": "Apple Inc."}
