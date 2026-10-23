"""price_threshold watches must reject blank/sentinel symbols and empty operators."""

import pytest

from src.storage.alert_watches import InvalidAlertWatchConfig, validate_watches_config
from src.storage.database import get_connection, init_database
from src.storage.user_alerts import save_user_alerts_config
from src.storage.users import create_user


@pytest.fixture
def db_user(tmp_path, monkeypatch):
    db_path = tmp_path / "blank-symbol.db"
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    init_database()
    return create_user("blank-symbol@example.com", "password123")["id"]


def _config(symbol="AAPL", operator="less_than", value=150):
    return {
        "defaults": {},
        "alerts": [
            {
                "id": "aapl-low",
                "enabled": True,
                "condition": {
                    "type": "price_threshold",
                    "symbol": symbol,
                    "operator": operator,
                    "value": value,
                },
            }
        ],
    }


@pytest.mark.parametrize(
    "symbol",
    ["", "   ", "\t", "nan", "NaN", "NONE", "null", "INF", None],
)
def test_validate_rejects_blank_and_sentinel_symbols(db_user, symbol):
    with pytest.raises(InvalidAlertWatchConfig, match="valid symbol"):
        validate_watches_config(db_user, _config(symbol=symbol))


@pytest.mark.parametrize("operator", [None, "", "   ", "\t"])
def test_validate_rejects_missing_or_blank_operator(db_user, operator):
    with pytest.raises(InvalidAlertWatchConfig, match="operator"):
        validate_watches_config(db_user, _config(operator=operator))


def test_save_rejects_sentinel_symbol_without_persisting(db_user):
    with pytest.raises(InvalidAlertWatchConfig, match="valid symbol"):
        save_user_alerts_config(db_user, _config(symbol="nan"))

    # Prior empty config remains empty — no partial zombie watch row.

    with get_connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM alert_watches WHERE user_id = ?",
            (db_user,),
        ).fetchone()["n"]
    assert count == 0


def test_validate_still_accepts_unsupported_operator_strings(db_user):
    """Unsupported ops soft-fail at eval (#339); save must not start rejecting them."""
    validate_watches_config(db_user, _config(operator="below"))
