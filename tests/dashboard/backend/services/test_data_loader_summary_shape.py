"""load_summary must reject missing dates so market summary stays 404 not 500."""

import shutil
import tempfile
from pathlib import Path

import pytest

from dashboard.backend.services.data_loader import DataLoader
from tests.helpers.market_bars import seed_summary


@pytest.fixture
def temp_data_dir(monkeypatch):
    monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)
    tmp = tempfile.mkdtemp()
    yield Path(tmp)
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def loader(temp_data_dir):
    return DataLoader(data_dir=temp_data_dir)


def test_load_summary_raises_when_missing(loader) -> None:
    with pytest.raises(ValueError, match="No summary files found"):
        loader.load_summary()


def test_load_summary_still_returns_object(loader, temp_data_dir) -> None:
    summary = {"date": "2026-01-15", "ai_summary": "ok"}
    seed_summary(temp_data_dir, "2026-01-15", summary)
    loaded = loader.load_summary()
    assert loaded["date"] == "2026-01-15"
    assert loaded["ai_summary"] == "ok"
