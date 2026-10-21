"""Encryption at rest for secrets held in the database.

IdP client secrets, SCIM bearer tokens and device push tokens are credentials.
Storing them in plaintext columns fails the first security review, and it means
a database dump is a full compromise of every customer's identity provider.

Keys are derived per tenant from a single master key, so:

- one tenant's ciphertext cannot be decrypted with another tenant's key, which
  limits the blast radius of a leaked key;
- there is still only one secret to rotate and protect operationally.

The master key comes from `SECRET_ENCRYPTION_KEY`, or falls back to
`JWT_SECRET_KEY` in development. In production a distinct key is required and
startup refuses without one — reusing the token-signing key for data at rest
means rotating one forces rotating the other.
"""

from __future__ import annotations

import base64
import hashlib
import uuid

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings

__all__ = [
    "EncryptionError",
    "can_encrypt",
    "decrypt_field",
    "decrypt_secret",
    "encrypt_field",
    "encrypt_secret",
    "is_encrypted",
]

# Marks a value as produced by this module, so a plaintext value written before
# encryption existed can be detected rather than mangled.
_PREFIX = "enc:v1:"


class EncryptionError(RuntimeError):
    """A secret could not be encrypted or decrypted."""


def _master_key() -> bytes:
    key = getattr(settings, "SECRET_ENCRYPTION_KEY", None) or ""
    if not key:
        if settings.is_production:
            raise EncryptionError(
                "SECRET_ENCRYPTION_KEY is not set. Refusing to encrypt tenant secrets "
                "with the JWT signing key in production — rotating one would force "
                "rotating the other."
            )
        key = settings.JWT_SECRET_KEY
    return key.encode("utf-8")


def _tenant_key(tenant_id: uuid.UUID) -> bytes:
    """Derive this tenant's Fernet key from the master key.

    HKDF-style derivation over the tenant id: deterministic, so the same
    ciphertext always decrypts, and one-way, so a derived key does not reveal
    the master.
    """
    derived = hashlib.pbkdf2_hmac(
        "sha256",
        _master_key(),
        b"askmira-tenant-secret:" + str(tenant_id).encode("ascii"),
        # A low count on purpose: this is key *derivation* from an already
        # high-entropy master key, not password stretching. The master key is
        # 64 random bytes, so there is nothing to brute-force.
        iterations=1_000,
        dklen=32,
    )
    return base64.urlsafe_b64encode(derived)


def can_encrypt() -> bool:
    """Whether this server can encrypt secrets at rest (a master key is configured)."""
    try:
        _master_key()
    except EncryptionError:
        return False
    return True


def is_encrypted(value: str | None) -> bool:
    return bool(value) and value.startswith(_PREFIX)


def encrypt_secret(value: str, tenant_id: uuid.UUID) -> str:
    """Encrypt a secret for storage.

    Returns a prefixed, URL-safe string. Encrypting an already-encrypted value
    is a no-op rather than double-encryption, so a re-save of an unchanged form
    field does not corrupt the stored credential.
    """
    if not value:
        return value
    if is_encrypted(value):
        return value

    token = Fernet(_tenant_key(tenant_id)).encrypt(value.encode("utf-8"))
    return _PREFIX + token.decode("ascii")


def decrypt_secret(value: str | None, tenant_id: uuid.UUID) -> str | None:
    """Decrypt a stored secret.

    Raises:
        EncryptionError: if the ciphertext does not belong to this tenant, or
            the master key has changed. Both are worth failing loudly on — a
            silently empty client secret produces an SSO outage that looks like
            a provider problem.
    """
    if value is None or value == "":
        return value
    if not is_encrypted(value):
        # A value written before this module existed. Return it, but the caller
        # should re-save to migrate it.
        return value

    try:
        return (
            Fernet(_tenant_key(tenant_id))
            .decrypt(value[len(_PREFIX) :].encode("ascii"))
            .decode("utf-8")
        )
    except (InvalidToken, ValueError) as exc:
        raise EncryptionError(
            "A stored secret could not be decrypted. Either it belongs to a different "
            "tenant, or SECRET_ENCRYPTION_KEY has changed since it was written."
        ) from exc


# ── Personal identifiers in ordinary columns ─────────────────────────────────
#
# A PAN or a bank account number on the employee row is read in a dozen places
# (payslips, Form 16, 24Q, the bank file) and is never searched by value. So it
# is encrypted by its column type, transparently, with a platform field key:
# a column type sees one value at a time and not the row's tenant. A database
# dump or a backup then holds ciphertext; the application reads plaintext.

_FIELD_PREFIX = "encf:v1:"


def _field_key() -> bytes:
    derived = hashlib.pbkdf2_hmac(
        "sha256", _master_key(), b"askmira-field-encryption:v1", iterations=1_000, dklen=32
    )
    return base64.urlsafe_b64encode(derived)


def encrypt_field(value: str) -> str:
    """Encrypt a personal identifier for its column. Idempotent on ciphertext."""
    if not value or value.startswith(_FIELD_PREFIX):
        return value
    token = Fernet(_field_key()).encrypt(value.encode("utf-8"))
    return _FIELD_PREFIX + token.decode("ascii")


def decrypt_field(value: str | None) -> str | None:
    """The identifier as written. A value stored before encryption comes back unchanged."""
    if not value or not value.startswith(_FIELD_PREFIX):
        return value
    try:
        token = value[len(_FIELD_PREFIX) :].encode("ascii")
        return Fernet(_field_key()).decrypt(token).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise EncryptionError(
            "A stored identifier could not be decrypted: SECRET_ENCRYPTION_KEY has changed "
            "since it was written."
        ) from exc
