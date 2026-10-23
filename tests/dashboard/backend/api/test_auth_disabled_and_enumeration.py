"""Single-user mode and recovery probes must fail closed without leaking accounts."""

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.storage.database import init_database


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def hosted_client(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "MARKETHELM_DATABASE_URL",
        f"sqlite:///{(tmp_path / 'enum.db').as_posix()}",
    )
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
        ("post", "/api/auth/verify-email/request", {"email": "a@example.com"}),
        ("post", "/api/auth/verify-email/confirm", {"token": "x" * 20}),
        ("post", "/api/auth/password-reset/request", {"email": "a@example.com"}),
        (
            "post",
            "/api/auth/password-reset/confirm",
            {"token": "x" * 20, "password": "new-password-123"},
        ),
    ],
)
def test_remaining_auth_routes_are_disabled_without_database(
    client, monkeypatch, method, path, json_body
):
    monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)
    kwargs = {}
    if json_body is not None:
        kwargs["json"] = json_body
    response = client.request(method.upper(), path, **kwargs)
    assert response.status_code == 501
    assert "Multi-user mode is disabled" in response.json()["detail"]


@pytest.mark.parametrize(
    "email",
    [
        "missing@example.com",
        "not-an-email",
        "a@b.com\ncc:evil@x.com",
        "@",
    ],
)
def test_password_reset_request_is_generic_for_unknown_and_junk_emails(hosted_client, email):
    known_shape = hosted_client.post(
        "/api/auth/password-reset/request", json={"email": "nobody@example.com"}
    )
    response = hosted_client.post("/api/auth/password-reset/request", json={"email": email})
    assert known_shape.status_code == response.status_code == 200
    assert known_shape.json() == response.json()
    assert "If the account exists" in response.json()["message"]


@pytest.mark.parametrize(
    "email",
    [
        "missing@example.com",
        "not-an-email",
        "a@b.com\ncc:evil@x.com",
        "@",
    ],
)
def test_verify_email_request_is_generic_for_unknown_and_junk_emails(hosted_client, email):
    known_shape = hosted_client.post(
        "/api/auth/verify-email/request", json={"email": "nobody@example.com"}
    )
    response = hosted_client.post("/api/auth/verify-email/request", json={"email": email})
    assert known_shape.status_code == response.status_code == 200
    assert known_shape.json() == response.json()
    assert "If the account exists" in response.json()["message"]


def test_login_junk_email_matches_wrong_password_401(hosted_client):
    registered = hosted_client.post(
        "/api/auth/register",
        json={"email": "login-enum@example.com", "password": "password123"},
    )
    assert registered.status_code == 200
    wrong_password = hosted_client.post(
        "/api/auth/login",
        json={"email": "login-enum@example.com", "password": "wrong-password"},
    )
    junk = hosted_client.post(
        "/api/auth/login",
        json={"email": "a@b.com\ncc:evil@x.com", "password": "password123"},
    )
    assert wrong_password.status_code == junk.status_code == 401
    assert wrong_password.json() == junk.json()
    assert wrong_password.json()["detail"] == "Invalid email or password."
