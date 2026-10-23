"""PUT /api/alerts/config must reject Inf/NaN price thresholds with HTTP 400."""

import json

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.storage.database import init_database


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def multi_user_env(tmp_path, monkeypatch):
    db_path = tmp_path / "threshold-api.db"
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("MARKETHELM_AUTH_SECRET", "test-secret-min-16-chars")

    init_database()


def _register(client, email: str = "threshold-api@example.com") -> str:
    r = client.post(
        "/api/auth/register",
        json={"email": email, "password": "password123"},
    )
    assert r.status_code == 200
    return r.json()["access_token"]


def _payload(value):
    return {
        "defaults": {},
        "alerts": [
            {
                "id": "aapl-low",
                "name": "AAPL low",
                "enabled": True,
                "cooldown_minutes": 15,
                "condition": {
                    "type": "price_threshold",
                    "symbol": "AAPL",
                    "operator": "less_than",
                    "value": value,
                },
                "notifications": ["log"],
            }
        ],
    }


@pytest.mark.parametrize(
    ("raw_token", "email"),
    [
        ("Infinity", "threshold-inf@example.com"),
        ("-Infinity", "threshold-ninf@example.com"),
        ("NaN", "threshold-nan@example.com"),
    ],
)
def test_hosted_put_rejects_nonfinite_price_threshold(client, multi_user_env, raw_token, email):
    token = _register(client, email)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    # httpx refuses to serialize Inf/NaN; send Python-json non-finite literals
    # the way a permissive client / proxy might.
    body = json.dumps(_payload(0)).replace(
        '"value": 0',
        f'"value": {raw_token}',
    )

    response = client.put("/api/alerts/config", content=body, headers=headers)

    assert response.status_code == 400
    assert "price threshold" in response.json()["detail"]

    saved = client.get("/api/alerts/config", headers={"Authorization": f"Bearer {token}"})
    assert saved.status_code == 200
    assert saved.json()["exists"] is False
    assert saved.json()["config"]["alerts"] == []


@pytest.mark.parametrize(
    "bad_value",
    ["Infinity", "-Infinity", "NaN"],
)
def test_hosted_put_rejects_nonfinite_threshold_string_tokens(client, multi_user_env, bad_value):
    """String tokens that float() treats as Inf/NaN must also fail closed."""
    token = _register(client, f"threshold-str-{bad_value.lower()}@example.com")
    headers = {"Authorization": f"Bearer {token}"}

    response = client.put(
        "/api/alerts/config",
        json=_payload(bad_value),
        headers=headers,
    )

    assert response.status_code == 400
    assert "price threshold" in response.json()["detail"]
    saved = client.get("/api/alerts/config", headers=headers)
    assert saved.status_code == 200
    assert saved.json()["exists"] is False


def test_hosted_put_nonfinite_threshold_preserves_existing_config(client, multi_user_env):
    token = _register(client, "threshold-preserve@example.com")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    ok = client.put(
        "/api/alerts/config",
        json=_payload(150),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert ok.status_code == 200
    assert ok.json()["config"]["alerts"][0]["condition"]["value"] == 150

    body = json.dumps(_payload(0)).replace('"value": 0', '"value": Infinity')
    bad = client.put("/api/alerts/config", content=body, headers=headers)

    assert bad.status_code == 400
    assert "price threshold" in bad.json()["detail"]

    saved = client.get("/api/alerts/config", headers={"Authorization": f"Bearer {token}"})
    assert saved.status_code == 200
    assert saved.json()["exists"] is True
    assert saved.json()["config"]["alerts"][0]["condition"]["value"] == 150


def test_hosted_put_accepts_finite_price_threshold(client, multi_user_env):
    token = _register(client, "threshold-ok@example.com")
    headers = {"Authorization": f"Bearer {token}"}

    response = client.put(
        "/api/alerts/config",
        json=_payload(42.5),
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["config"]["alerts"][0]["condition"]["value"] == 42.5
