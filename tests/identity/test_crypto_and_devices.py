"""Slice 04: secret encryption and device payload normalisation."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.core.crypto import EncryptionError, decrypt_secret, encrypt_secret, is_encrypted
from app.models.attendance import PunchDirection
from app.models.identity import DeviceVendor
from app.services.identity.devices import DeviceError, normalise

TENANT = uuid.uuid4()
OTHER_TENANT = uuid.uuid4()


# ═══════════════════════════════════════════════════════════════════════════
# Encryption at rest
# ═══════════════════════════════════════════════════════════════════════════


def test_a_secret_round_trips() -> None:
    secret = "super-secret-oidc-client-value"
    stored = encrypt_secret(secret, TENANT)

    assert stored != secret
    assert secret not in stored
    assert decrypt_secret(stored, TENANT) == secret


def test_ciphertext_is_marked_so_plaintext_is_distinguishable() -> None:
    stored = encrypt_secret("value", TENANT)
    assert is_encrypted(stored)
    assert not is_encrypted("value")


def test_one_tenants_ciphertext_cannot_be_read_with_anothers_key() -> None:
    """Per-tenant derivation is what limits the blast radius of a leaked key."""
    stored = encrypt_secret("acme-client-secret", TENANT)

    with pytest.raises(EncryptionError, match="different tenant"):
        decrypt_secret(stored, OTHER_TENANT)


def test_encrypting_twice_does_not_double_encrypt() -> None:
    """Re-saving an unchanged form field must not corrupt the credential."""
    once = encrypt_secret("value", TENANT)
    twice = encrypt_secret(once, TENANT)

    assert once == twice
    assert decrypt_secret(twice, TENANT) == "value"


def test_encryption_is_not_deterministic() -> None:
    """Identical secrets must not produce identical ciphertext.

    Otherwise a database dump reveals which tenants share a credential.
    """
    assert encrypt_secret("same", TENANT) != encrypt_secret("same", TENANT)


def test_empty_and_none_pass_through() -> None:
    assert encrypt_secret("", TENANT) == ""
    assert decrypt_secret(None, TENANT) is None
    assert decrypt_secret("", TENANT) == ""


def test_a_legacy_plaintext_value_is_returned_rather_than_mangled() -> None:
    """A value written before this module existed still reads back."""
    assert decrypt_secret("legacy-plaintext", TENANT) == "legacy-plaintext"


# ═══════════════════════════════════════════════════════════════════════════
# Device normalisation
# ═══════════════════════════════════════════════════════════════════════════


def test_an_essl_payload_normalises() -> None:
    punch = normalise(
        {"UserID": "ACM001", "LogTime": "2026-04-06 09:15:00", "Status": "0"},
        vendor=DeviceVendor.ESSL,
        timezone_name="Asia/Kolkata",
    )

    assert punch.employee_code == "ACM001"
    assert punch.direction is PunchDirection.IN
    # 09:15 IST is 03:45 UTC.
    assert punch.punched_at.hour == 3
    assert punch.punched_at.minute == 45


def test_a_zkteco_payload_normalises() -> None:
    punch = normalise(
        {"pin": "ACM002", "timestamp": "2026-04-06 18:05:00", "state": "1"},
        vendor=DeviceVendor.ZKTECO,
        timezone_name="Asia/Kolkata",
    )

    assert punch.employee_code == "ACM002"
    assert punch.direction is PunchDirection.OUT


def test_a_matrix_payload_normalises() -> None:
    punch = normalise(
        {"UserId": "ACM003", "EventDateTime": "06-04-2026 09:00:00", "EventType": "in"},
        vendor=DeviceVendor.MATRIX,
        timezone_name="Asia/Kolkata",
    )
    assert punch.employee_code == "ACM003"
    assert punch.direction is PunchDirection.IN


def test_field_lookup_is_case_insensitive() -> None:
    """Device firmware is inconsistent about capitalisation."""
    punch = normalise(
        {"userid": "ACM004", "logtime": "2026-04-06 09:00:00", "status": "0"},
        vendor=DeviceVendor.ESSL,
        timezone_name="Asia/Kolkata",
    )
    assert punch.employee_code == "ACM004"


def test_a_naive_timestamp_is_interpreted_in_the_device_timezone() -> None:
    punch = normalise(
        {"employee_code": "X", "timestamp": "2026-04-06 09:00:00"},
        vendor=DeviceVendor.GENERIC,
        timezone_name="Asia/Kolkata",
    )
    assert punch.punched_at.tzinfo is not None
    assert punch.punched_at == datetime(2026, 4, 6, 3, 30, tzinfo=UTC)


def test_the_timezone_assumption_is_recorded_on_the_punch() -> None:
    """When a punch looks an hour wrong, the evidence has to be on the record."""
    punch = normalise(
        {"employee_code": "X", "timestamp": "2026-04-06 09:00:00"},
        vendor=DeviceVendor.GENERIC,
        timezone_name="Asia/Kolkata",
    )
    marker = punch.raw["_askmira"]

    assert marker["assumed_timezone"] == "Asia/Kolkata"
    assert marker["timezone_was_assumed"] is True
    assert marker["vendor"] == "generic"


def test_a_timestamp_with_an_offset_is_not_assumed() -> None:
    punch = normalise(
        {"employee_code": "X", "timestamp": "2026-04-06T09:00:00+05:30"},
        vendor=DeviceVendor.GENERIC,
        timezone_name="Asia/Kolkata",
    )
    assert punch.raw["_askmira"]["timezone_was_assumed"] is False
    assert punch.punched_at == datetime(2026, 4, 6, 3, 30, tzinfo=UTC)


def test_epoch_seconds_are_unambiguous() -> None:
    punch = normalise(
        {"employee_code": "X", "timestamp": 1775458800},
        vendor=DeviceVendor.GENERIC,
        timezone_name="Asia/Kolkata",
    )
    assert punch.punched_at.tzinfo is UTC
    assert punch.raw["_askmira"]["timezone_was_assumed"] is False


def test_an_unknown_timezone_falls_back_rather_than_failing() -> None:
    punch = normalise(
        {"employee_code": "X", "timestamp": "2026-04-06 09:00:00"},
        vendor=DeviceVendor.GENERIC,
        timezone_name="Mars/Olympus_Mons",
    )
    assert punch.punched_at == datetime(2026, 4, 6, 3, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    "raw_time",
    ["2026-04-06 09:00:00", "06-04-2026 09:00:00", "2026/04/06 09:00:00", "2026-04-06T09:00:00"],
)
def test_common_device_date_formats_all_parse(raw_time: str) -> None:
    punch = normalise(
        {"employee_code": "X", "timestamp": raw_time},
        vendor=DeviceVendor.GENERIC,
        timezone_name="Asia/Kolkata",
    )
    assert punch.punched_at.date().isoformat() == "2026-04-06"


def test_a_payload_with_no_employee_code_raises() -> None:
    with pytest.raises(DeviceError, match="no employee code"):
        normalise(
            {"timestamp": "2026-04-06 09:00:00"},
            vendor=DeviceVendor.GENERIC,
            timezone_name="Asia/Kolkata",
        )


def test_a_payload_with_no_timestamp_raises() -> None:
    with pytest.raises(DeviceError, match="no timestamp"):
        normalise(
            {"employee_code": "X"},
            vendor=DeviceVendor.GENERIC,
            timezone_name="Asia/Kolkata",
        )


def test_an_unreadable_timestamp_raises() -> None:
    with pytest.raises(DeviceError, match="Could not read the timestamp"):
        normalise(
            {"employee_code": "X", "timestamp": "yesterday afternoon"},
            vendor=DeviceVendor.GENERIC,
            timezone_name="Asia/Kolkata",
        )


def test_an_unrecognised_direction_defaults_to_in() -> None:
    """A missing OUT surfaces as an unpaired punch needing regularisation.

    That is visible and fixable. Guessing OUT would silently halve someone's
    worked hours.
    """
    punch = normalise(
        {"employee_code": "X", "timestamp": "2026-04-06 09:00:00", "direction": "???"},
        vendor=DeviceVendor.GENERIC,
        timezone_name="Asia/Kolkata",
    )
    assert punch.direction is PunchDirection.IN


def test_the_full_raw_payload_is_preserved_for_debugging() -> None:
    payload = {"employee_code": "X", "timestamp": "2026-04-06 09:00:00", "odd_field": 42}
    punch = normalise(
        payload, vendor=DeviceVendor.GENERIC, timezone_name="Asia/Kolkata"
    )
    assert punch.raw["odd_field"] == 42
