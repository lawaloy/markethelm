"""Unit tests for signed access tokens."""

import base64
import hashlib
import hmac
import json
import time

import pytest

from src.storage import session as session_mod
from src.storage.session import (
    AuthError,
    create_access_token,
    decode_access_token,
    ensure_auth_secret,
)


@pytest.fixture
def auth_secret(monkeypatch):
    monkeypatch.setenv("MARKETHELM_AUTH_SECRET", "test-secret-min-16-chars")


class TestSession:
    def test_create_and_decode_token(self, auth_secret):
        token = create_access_token("user-123")
        payload = decode_access_token(token)
        assert payload["user_id"] == "user-123"
        assert payload["session_version"] == 1

    def test_session_version_round_trip(self, auth_secret):
        token = create_access_token("user-123", session_version=7)
        assert decode_access_token(token)["session_version"] == 7

    @pytest.mark.parametrize("version", [0, -1, True, 1.5, "1"])
    def test_invalid_session_version_rejected(self, auth_secret, version):
        with pytest.raises(AuthError, match="session version"):
            create_access_token("user-123", session_version=version)

    def test_expired_token_rejected(self, auth_secret, monkeypatch):
        monkeypatch.setattr(time, "time", lambda: 1_000_000)
        token = create_access_token("user-123", ttl_seconds=1)
        monkeypatch.setattr(time, "time", lambda: 1_000_010)
        with pytest.raises(AuthError, match="expired"):
            decode_access_token(token)

    def test_exact_expiry_boundary_rejected(self, auth_secret, monkeypatch):
        """Tokens with exp == now must be treated as expired (zero TTL)."""
        monkeypatch.setattr(time, "time", lambda: 1_000_000)
        token = create_access_token("user-123", ttl_seconds=0)
        with pytest.raises(AuthError, match="expired"):
            decode_access_token(token)

    def test_invalid_signature_rejected(self, auth_secret):
        token = create_access_token("user-123")
        tampered = token[:-4] + "xxxx"
        with pytest.raises(AuthError):
            decode_access_token(tampered)

    def test_wrong_secret_rejected(self, auth_secret, monkeypatch):
        token = create_access_token("user-123")
        monkeypatch.setenv("MARKETHELM_AUTH_SECRET", "rotated-secret-16c")
        with pytest.raises(AuthError, match="signature"):
            decode_access_token(token)

    @pytest.mark.parametrize(
        "token",
        [
            "not-a-token",
            ".",
            "abc.",
            ".sig",
            "%%%invalid%%%." + "aaaa",
        ],
    )
    def test_malformed_token_rejected(self, auth_secret, token):
        with pytest.raises(AuthError):
            decode_access_token(token)

    def test_blank_or_missing_subject_rejected(self, auth_secret):
        def _sign(payload: dict) -> str:
            body = (
                base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode())
                .decode()
                .rstrip("=")
            )
            sig = hmac.new(
                session_mod._auth_secret(),
                body.encode("ascii"),
                hashlib.sha256,
            ).digest()
            return f"{body}.{base64.urlsafe_b64encode(sig).decode().rstrip('=')}"

        blank = _sign({"sub": "", "exp": int(time.time()) + 60})
        missing = _sign({"exp": int(time.time()) + 60})
        with pytest.raises(AuthError, match="subject"):
            decode_access_token(blank)
        with pytest.raises(AuthError, match="subject"):
            decode_access_token(missing)

    def test_short_secret_rejected(self, monkeypatch):
        monkeypatch.setenv("MARKETHELM_AUTH_SECRET", "too-short")
        with pytest.raises(AuthError, match="MARKETHELM_AUTH_SECRET"):
            create_access_token("user-123")
        with pytest.raises(AuthError, match="MARKETHELM_AUTH_SECRET"):
            ensure_auth_secret()

    def test_missing_secret_raises(self, monkeypatch):
        monkeypatch.delenv("MARKETHELM_AUTH_SECRET", raising=False)
        with pytest.raises(AuthError, match="MARKETHELM_AUTH_SECRET"):
            create_access_token("user-123")
        with pytest.raises(AuthError, match="MARKETHELM_AUTH_SECRET"):
            ensure_auth_secret()
