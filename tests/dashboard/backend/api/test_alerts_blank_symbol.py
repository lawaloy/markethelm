"""PUT /api/alerts/config must reject blank/sentinel symbols and empty operators."""

import json

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.storage.database import init_database


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def file_mode(tmp_path, monkeypatch):
    monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)
    config_path = tmp_path / "alerts.json"
    monkeypatch.setenv("MARKETHELM_ALERTS_CONFIG", str(config_path))
    return config_path


@pytest.fixture
def multi_user_env(tmp_path, monkeypatch):
    db_path = tmp_path / "blank-symbol-api.db"
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("MARKETHELM_AUTH_SECRET", "test-secret-min-16-chars")

    init_database()


def _register(client, email: str) -> str:
    r = client.post(
        "/api/auth/register",
        json={"email": email, "password": "password123"},
    )
    assert r.status_code == 200
    return r.json()["access_token"]


def _payload(symbol="AAPL", operator="less_than", value=150):
    return {
        "defaults": {},
        "alerts": [
            {
                "id": "aapl-low",
                "name": "AAPL low",
                "enabled": True,
                "condition": {
                    "type": "price_threshold",
                    "symbol": symbol,
                    "operator": operator,
                    "value": value,
                },
                "notifications": ["log"],
            }
        ],
    }


@pytest.mark.parametrize("symbol", ["", "   ", "nan", "NONE", "null"])
def test_file_mode_put_rejects_blank_or_sentinel_symbol(client, file_mode, symbol):
    response = client.put("/api/alerts/config", json=_payload(symbol=symbol))
    assert response.status_code == 400
    assert "symbol" in response.json()["detail"].lower()
    assert not file_mode.exists()


@pytest.mark.parametrize("operator", [None, "", "   "])
def test_file_mode_put_rejects_blank_operator(client, file_mode, operator):
    payload = _payload()
    if operator is None:
        del payload["alerts"][0]["condition"]["operator"]
    else:
        payload["alerts"][0]["condition"]["operator"] = operator
    response = client.put("/api/alerts/config", json=payload)
    assert response.status_code == 400
    assert "operator" in response.json()["detail"].lower()
    assert not file_mode.exists()


@pytest.mark.parametrize(
    ("symbol", "email"),
    [
        ("", "blank-sym-empty@example.com"),
        ("   ", "blank-sym-spaces@example.com"),
        ("nan", "blank-sym-nan@example.com"),
        ("NONE", "blank-sym-none@example.com"),
    ],
)
def test_hosted_put_rejects_blank_or_sentinel_symbol(client, multi_user_env, symbol, email):
    token = _register(client, email)
    headers = {"Authorization": f"Bearer {token}"}

    response = client.put("/api/alerts/config", json=_payload(symbol=symbol), headers=headers)
    assert response.status_code == 400
    assert "symbol" in response.json()["detail"].lower()

    saved = client.get("/api/alerts/config", headers=headers)
    assert saved.status_code == 200
    assert saved.json()["config"]["alerts"] == []


def test_hosted_put_rejects_blank_operator_and_preserves_config(client, multi_user_env):
    token = _register(client, "blank-op-preserve@example.com")
    headers = {"Authorization": f"Bearer {token}"}

    ok = client.put("/api/alerts/config", json=_payload(symbol="AAPL"), headers=headers)
    assert ok.status_code == 200

    bad = client.put(
        "/api/alerts/config",
        json=_payload(symbol="MSFT", operator="   "),
        headers=headers,
    )
    assert bad.status_code == 400
    assert "operator" in bad.json()["detail"].lower()

    saved = client.get("/api/alerts/config", headers=headers)
    assert saved.status_code == 200
    assert saved.json()["config"]["alerts"][0]["condition"]["symbol"] == "AAPL"


def test_file_mode_put_normalizes_padded_symbol(client, file_mode):
    response = client.put("/api/alerts/config", json=_payload(symbol=" aapl "))
    assert response.status_code == 200
    assert response.json()["config"]["alerts"][0]["condition"]["symbol"] == "AAPL"
    on_disk = json.loads(file_mode.read_text())
    assert on_disk["alerts"][0]["condition"]["symbol"] == "AAPL"
