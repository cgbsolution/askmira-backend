"""Password hashing and JWT minting/verification.

Auth is entirely our own: Supabase supplies the Postgres instance and nothing
else. Every token in this system is signed and verified here.

Argon2id rather than bcrypt — no 72-byte input truncation to work around, and
the cost parameters are tunable from config as hardware improves.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.config import settings

TokenType = Literal["access", "refresh", "reset", "verify"]

_hasher = PasswordHasher(
    time_cost=settings.ARGON2_TIME_COST,
    memory_cost=settings.ARGON2_MEMORY_COST,
    parallelism=settings.ARGON2_PARALLELISM,
)


# ── Passwords ───────────────────────────────────────────────────────────────


def hash_password(password: str) -> str:
    """Return an Argon2id hash. The salt is generated per call and embedded."""
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    return True


def needs_rehash(password_hash: str) -> bool:
    """True when the hash was made with weaker parameters than we now use.

    Call after a successful verify: re-hash transparently so old accounts
    migrate to stronger parameters without a forced password reset.
    """
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


# ── Tokens ──────────────────────────────────────────────────────────────────


def _encode(payload: dict[str, Any], expires: timedelta, token_type: TokenType) -> str:
    now = datetime.now(UTC)
    claims = {
        **payload,
        "typ": token_type,
        "iat": now,
        "exp": now + expires,
        "jti": str(uuid.uuid4()),
        "iss": settings.APP_NAME,
    }
    return jwt.encode(claims, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def create_access_token(*, user_id: str, tenant_id: str | None, role: str) -> str:
    """Short-lived token carrying the identity used for every authorised call.

    `tid` is the only source of truth for tenant scoping — a request body may
    never override it.
    """
    return _encode(
        {"sub": user_id, "tid": tenant_id, "role": role},
        timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
        "access",
    )


def create_refresh_token(*, user_id: str, tenant_id: str | None) -> str:
    return _encode(
        {"sub": user_id, "tid": tenant_id},
        timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
        "refresh",
    )


def create_single_use_token(
    *, user_id: str, token_type: Literal["reset", "verify"], expires_in_hours: int = 24
) -> str:
    """Password-reset and email-verification tokens.

    Expiry is intrinsic to the token, so there is no table of opaque tokens to
    keep clean.
    """
    return _encode({"sub": user_id}, timedelta(hours=expires_in_hours), token_type)


def decode_token(token: str, *, expected_type: TokenType) -> dict[str, Any]:
    """Decode and validate a token, or raise.

    Raises:
        jwt.ExpiredSignatureError: the token is past its `exp`.
        jwt.InvalidTokenError: signature, issuer or `typ` mismatch.
    """
    payload: dict[str, Any] = jwt.decode(
        token,
        settings.JWT_SECRET_KEY,
        algorithms=[settings.JWT_ALGORITHM],
        issuer=settings.APP_NAME,
    )
    if payload.get("typ") != expected_type:
        raise jwt.InvalidTokenError(
            f"expected a {expected_type} token, got {payload.get('typ')!r}"
        )
    return payload
