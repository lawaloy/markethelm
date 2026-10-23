"""Hosted GET /api/alerts/config must soft-fail corrupt stored JSON."""

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.storage.database import get_connection, init_database


@pytest.fixture
def multi_user_env(tmp_path, monkeypatch):
    db_path = tmp_path / "corrupt-config.db"
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("MARKETHELM_AUTH_SECRET", "test-secret-min-16-chars")

    init_database()


@pytest.fixture
def client():
    return TestClient(app)


def _register(client, email: str = "corrupt@example.com") -> tuple[str, str]:
    r = client.post(
        "/api/auth/register",
        json={"email": email, "password": "password123"},
    )
    assert r.status_code == 200
    body = r.json()
    return body["access_token"], body["user"]["id"]


@pytest.mark.parametrize(
    "blob",
    ['{"not": "recoverable"', "[]", '"just-a-string"', "null"],
)
def test_get_config_soft_fails_corrupt_hosted_row(client, multi_user_env, blob) -> None:
    token, user_id = _register(client)
    headers = {"Authorization": f"Bearer {token}"}

    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO user_alert_configs (user_id, config_json, updated_at)
            VALUES (?, ?, ?)
            """,
            (user_id, blob, "2026-07-24T00:00:00+00:00"),
        )

    response = client.get("/api/alerts/config", headers=headers)

    assert response.status_code == 200
    payload = response.json()
    assert payload["exists"] is True
    assert payload["config"]["alerts"] == []
    assert isinstance(payload["config"]["defaults"], dict)
