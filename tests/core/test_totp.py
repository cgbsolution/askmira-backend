"""TOTP against the RFC 6238 test vectors, and the replay and drift rules."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

from app.core import totp

#: RFC 6238 appendix B: the SHA-1 seed is the ASCII "12345678901234567890".
RFC_SECRET = base64.b32encode(b"12345678901234567890").decode().rstrip("=")


def _at(seconds: int) -> datetime:
    return datetime.fromtimestamp(seconds, UTC)


def test_rfc_6238_vectors_six_digits() -> None:
    # The RFC lists eight digits; six-digit codes are their last six.
    for seconds, eight in (
        (59, "94287082"),
        (1111111109, "07081804"),
        (1111111111, "14050471"),
        (1234567890, "89005924"),
        (2000000000, "69279037"),
    ):
        assert totp.code_at(RFC_SECRET, _at(seconds)) == eight[-6:]


def test_a_code_is_accepted_one_step_either_side() -> None:
    now = _at(1_700_000_000)
    early = totp.code_at(RFC_SECRET, now - timedelta(seconds=30))
    late = totp.code_at(RFC_SECRET, now + timedelta(seconds=30))
    stale = totp.code_at(RFC_SECRET, now - timedelta(seconds=90))
    assert totp.verify(RFC_SECRET, early, moment=now) is not None
    assert totp.verify(RFC_SECRET, late, moment=now) is not None
    assert totp.verify(RFC_SECRET, stale, moment=now) is None


def test_a_used_code_cannot_be_replayed() -> None:
    now = _at(1_700_000_000)
    code = totp.code_at(RFC_SECRET, now)
    step = totp.verify(RFC_SECRET, code, moment=now)
    assert step is not None
    assert totp.verify(RFC_SECRET, code, moment=now, after_step=step) is None


def test_junk_is_refused_without_error() -> None:
    assert totp.verify(RFC_SECRET, "12a456") is None
    assert totp.verify(RFC_SECRET, "") is None
    assert totp.verify(RFC_SECRET, "1234567") is None


def test_spaces_in_a_typed_code_are_ignored() -> None:
    now = _at(1_700_000_000)
    code = totp.code_at(RFC_SECRET, now)
    assert totp.verify(RFC_SECRET, f"{code[:3]} {code[3:]}", moment=now) is not None


def test_new_secrets_are_distinct_and_decodable() -> None:
    one, two = totp.new_secret(), totp.new_secret()
    assert one != two and len(one) == 32
    assert totp.code_at(one, _at(0)).isdigit()


def test_the_provisioning_uri_names_issuer_and_account() -> None:
    uri = totp.provisioning_uri("ABC", account="priya@acme.in", issuer="AskMira")
    assert uri.startswith("otpauth://totp/AskMira%3Apriya%40acme.in?")
    assert "issuer=AskMira" in uri and "secret=ABC" in uri
