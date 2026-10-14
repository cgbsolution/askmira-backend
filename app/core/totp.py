"""Time-based one-time passwords (RFC 6238), as authenticator apps compute them.

SHA-1, six digits, thirty-second steps: what Google Authenticator, Microsoft
Authenticator, Authy and 1Password all default to. Written against the RFC
rather than pulled in as a dependency because it is twenty lines of HMAC, and
the one subtle part — refusing a code already used — needs the caller's state
anyway (`verify` returns the step it matched so the caller can store it).

Pure.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
from datetime import UTC, datetime
from urllib.parse import quote, urlencode

__all__ = ["DIGITS", "STEP_SECONDS", "code_at", "new_secret", "provisioning_uri", "verify"]

DIGITS = 6
STEP_SECONDS = 30
#: Steps either side of now a code is accepted for: thirty seconds of clock
#: drift, or a code typed just as it rolled over.
DRIFT_STEPS = 1


def new_secret() -> str:
    """160 random bits, base32 without padding — the form apps expect."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _key(secret: str) -> bytes:
    padded = secret.upper() + "=" * (-len(secret) % 8)
    return base64.b32decode(padded)


def _step(moment: datetime) -> int:
    return int(moment.timestamp()) // STEP_SECONDS


def _hotp(key: bytes, counter: int) -> str:
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**DIGITS).zfill(DIGITS)


def code_at(secret: str, moment: datetime) -> str:
    return _hotp(_key(secret), _step(moment))


def verify(
    secret: str, code: str, *, moment: datetime | None = None, after_step: int | None = None
) -> int | None:
    """The step a code matches, or None.

    `after_step` is the last step this secret accepted: a code for that step or
    an earlier one is refused, so a code seen over somebody's shoulder cannot be
    replayed inside its thirty seconds.
    """
    cleaned = "".join(code.split())
    if len(cleaned) != DIGITS or not cleaned.isdigit():
        return None
    now = _step(moment or datetime.now(UTC))
    key = _key(secret)
    for step in range(now - DRIFT_STEPS, now + DRIFT_STEPS + 1):
        if after_step is not None and step <= after_step:
            continue
        if hmac.compare_digest(_hotp(key, step), cleaned):
            return step
    return None


def provisioning_uri(secret: str, *, account: str, issuer: str) -> str:
    """The otpauth:// link an authenticator app reads from a QR code."""
    label = quote(f"{issuer}:{account}")
    query = urlencode(
        {"secret": secret, "issuer": issuer, "digits": DIGITS, "period": STEP_SECONDS}
    )
    return f"otpauth://totp/{label}?{query}"
