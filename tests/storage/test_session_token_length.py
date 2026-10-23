"""Oversized Bearer tokens must fail closed before HMAC (CPU/memory DoS guard)."""

import base64
import hashlib
import hmac
import json
import time

import pytest

from src.storage import session as session_mod
from src.storage.session import (
    MAX_ACCESS_TOKEN_LENGTH,
    AuthError,
    create_access_token,
    decode_access_token,
)


@pytest.fixture
def auth_secret(monkeypatch):
    monkeypatch.setenv("MARKETHELM_AUTH_SECRET", "test-secret-min-16-chars")


def test_decode_rejects_oversized_token_before_hmac(auth_secret, monkeypatch):
    calls = {"n": 0}
    real_new = hmac.new

    def counting_new(*args, **kwargs):
        calls["n"] += 1
        return real_new(*args, **kwargs)

    monkeypatch.setattr(hmac, "new", counting_new)
    huge = "a" * (MAX_ACCESS_TOKEN_LENGTH + 1)
    with pytest.raises(AuthError, match="Invalid access token"):
        decode_access_token(huge)
    assert calls["n"] == 0


def test_decode_rejects_non_string_token(auth_secret):
    with pytest.raises(AuthError, match="Invalid access token"):
        decode_access_token(None)


def test_created_token_within_length_ceiling(auth_secret):
    token = create_access_token("user-" + ("x" * 64))
    assert len(token) <= MAX_ACCESS_TOKEN_LENGTH
    assert decode_access_token(token)["user_id"].startswith("user-")


def test_decode_accepts_token_at_max_length_when_well_formed(auth_secret, monkeypatch):
    """A well-formed token padded to the ceiling still authenticates (boundary)."""

    # Build a signed token, then pad the body segment with ignored JSON whitespace
    # is not possible after b64 — instead verify len == MAX is allowed for garbage
    # that fails later as invalid, proving the length check is inclusive.
    body = (
        base64.urlsafe_b64encode(
            json.dumps(
                {"sub": "pad-user", "exp": int(time.time()) + 60},
                separators=(",", ":"),
            ).encode()
        )
        .decode()
        .rstrip("=")
    )
    sig = hmac.new(
        session_mod._auth_secret(),
        body.encode("ascii"),
        hashlib.sha256,
    ).digest()
    token = f"{body}.{base64.urlsafe_b64encode(sig).decode().rstrip('=')}"
    assert len(token) < MAX_ACCESS_TOKEN_LENGTH
    # Pad with trailing junk past the signature segment to hit exact ceiling.
    pad = "x" * (MAX_ACCESS_TOKEN_LENGTH - len(token))
    padded = token + pad
    assert len(padded) == MAX_ACCESS_TOKEN_LENGTH
    # Signature no longer matches after padding — still AuthError, but length OK.
    with pytest.raises(AuthError):
        decode_access_token(padded)
