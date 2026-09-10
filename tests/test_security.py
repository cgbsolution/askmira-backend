"""Auth primitives — the parts that must not regress quietly."""

from __future__ import annotations

import time

import jwt
import pytest

from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)


def test_password_round_trip() -> None:
    hashed = hash_password("correct-horse-9")
    assert verify_password("correct-horse-9", hashed)
    assert not verify_password("wrong-horse-9", hashed)


def test_hash_is_salted() -> None:
    """The same password must not produce the same hash twice."""
    assert hash_password("same-password-1") != hash_password("same-password-1")


def test_verify_rejects_a_malformed_hash() -> None:
    """A corrupt stored hash returns False rather than raising."""
    assert not verify_password("anything", "not-a-real-argon2-hash")


def test_access_token_carries_tenant_and_role() -> None:
    token = create_access_token(user_id="u-1", tenant_id="t-1", role="hr_admin")
    payload = decode_token(token, expected_type="access")

    assert payload["sub"] == "u-1"
    assert payload["tid"] == "t-1"
    assert payload["role"] == "hr_admin"


def test_a_refresh_token_is_not_accepted_as_an_access_token() -> None:
    """Token type confusion would turn a 30-day token into a session token."""
    refresh = create_refresh_token(user_id="u-1", tenant_id="t-1")

    with pytest.raises(jwt.InvalidTokenError):
        decode_token(refresh, expected_type="access")


def test_a_tampered_token_is_rejected() -> None:
    token = create_access_token(user_id="u-1", tenant_id="t-1", role="employee")
    header, payload, signature = token.split(".")
    forged = f"{header}.{payload}.{signature[:-4]}AAAA"

    with pytest.raises(jwt.InvalidTokenError):
        decode_token(forged, expected_type="access")


def test_an_expired_token_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core import security

    monkeypatch.setattr(security.settings, "ACCESS_TOKEN_EXPIRE_MINUTES", -1)
    token = security.create_access_token(user_id="u-1", tenant_id="t-1", role="employee")

    with pytest.raises(jwt.ExpiredSignatureError):
        security.decode_token(token, expected_type="access")


def test_tokens_have_distinct_ids() -> None:
    """The `jti` is what makes a refresh token individually revocable."""
    first = decode_token(
        create_refresh_token(user_id="u-1", tenant_id="t-1"), expected_type="refresh"
    )
    time.sleep(0.01)
    second = decode_token(
        create_refresh_token(user_id="u-1", tenant_id="t-1"), expected_type="refresh"
    )
    assert first["jti"] != second["jti"]
