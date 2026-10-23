"""Account mutation and recovery confirm endpoints must fail closed without a session."""

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.storage.database import init_database


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", f"sqlite:///{(tmp_path / 'gates.db').as_posix()}")
    monkeypatch.setenv("MARKETHELM_AUTH_SECRET", "test-secret-min-16-chars")

    init_database()

    return TestClient(app)


@pytest.mark.parametrize(
    "method,path,json_body",
    [
        ("post", "/api/auth/logout", None),
        (
            "post",
            "/api/auth/password/change",
            {"current_password": "password123", "new_password": "new-password-123"},
        ),
        (
            "delete",
            "/api/auth/account",
            {"current_password": "password123", "confirmation": "DELETE"},
        ),
    ],
)
def test_account_mutations_require_authentication(client, method, path, json_body):
    response = client.request(method.upper(), path, json=json_body)
    assert response.status_code == 401
    assert response.json()["detail"] == "Authentication required."


@pytest.mark.parametrize(
    "path,json_body",
    [
        (
            "/api/auth/password-reset/confirm",
            {"token": "x" * 19, "password": "new-password-123"},
        ),
        (
            "/api/auth/verify-email/confirm",
            {"token": "x" * 19},
        ),
    ],
)
def test_confirm_endpoints_reject_short_tokens(client, path, json_body):
    response = client.post(path, json=json_body)
    assert response.status_code == 422


@pytest.mark.parametrize(
    "path,json_body,detail",
    [
        (
            "/api/auth/password-reset/confirm",
            {"token": "x" * 20, "password": "new-password-123"},
            "This reset link is invalid or expired.",
        ),
        (
            "/api/auth/verify-email/confirm",
            {"token": "x" * 20},
            "This verification link is invalid or expired.",
        ),
    ],
)
def test_confirm_endpoints_reject_unknown_min_length_tokens(client, path, json_body, detail):
    response = client.post(path, json=json_body)
    assert response.status_code == 400
    assert response.json()["detail"] == detail
