"""Statutory identifiers and the profile section contracts, without a database."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from pydantic import TypeAdapter, ValidationError

from app.core.crypto import encrypt_secret
from app.schemas.employee_profile import (
    DIRECT_SECTIONS,
    SENSITIVE_SECTIONS,
    Addresses,
    ProfileSection,
    SectionWrite,
    StatutoryDetails,
)
from app.services import employee_identifiers as ids
from app.services.employee_profile import _public, _stored_form

_INVERSE = (0, 4, 3, 2, 1, 5, 6, 7, 8, 9)


def _with_check_digit(eleven: str) -> str:
    """Append the Verhoeff check digit, so tests do not carry a real Aadhaar."""
    check = 0
    for position, char in enumerate(reversed(eleven)):
        check = ids._D[check][ids._P[(position + 1) % 8][int(char)]]
    return eleven + str(_INVERSE[check])


VALID_AADHAAR = _with_check_digit("23456789012")

# ── PAN ────────────────────────────────────────────────────────────────────


def test_a_well_formed_individual_pan_is_accepted_and_upper_cased() -> None:
    assert ids.validate_pan(" abcpe1234f ") == "ABCPE1234F"


@pytest.mark.parametrize("value", ["ABCPE1234", "ABCPE12345", "1BCPE1234F", "ABCPEX234F"])
def test_a_malformed_pan_is_refused(value: str) -> None:
    with pytest.raises(ids.IdentifierError):
        ids.validate_pan(value)


def test_a_company_pan_on_an_employee_is_refused_and_says_why() -> None:
    with pytest.raises(ids.IdentifierError, match="fourth character"):
        ids.validate_pan("ABCCE1234F")


# ── UAN, ESI, PF ───────────────────────────────────────────────────────────


def test_a_uan_is_twelve_digits() -> None:
    assert ids.validate_uan("1012 3456 7890") == "101234567890"
    with pytest.raises(ids.IdentifierError):
        ids.validate_uan("10123456789")
    with pytest.raises(ids.IdentifierError):
        ids.validate_uan("10123456789A")


def test_an_esi_number_is_ten_digits() -> None:
    assert ids.validate_esi("3100123456") == "3100123456"
    with pytest.raises(ids.IdentifierError):
        ids.validate_esi("31001234")


def test_a_pf_member_id_reads_like_region_office_establishment_account() -> None:
    assert ids.validate_pf_number("mh/ban/0012345/000/0000123") == "MH/BAN/0012345/000/0000123"
    with pytest.raises(ids.IdentifierError):
        ids.validate_pf_number("not a pf number")


# ── Aadhaar ────────────────────────────────────────────────────────────────


def test_a_valid_aadhaar_passes_its_check_digit() -> None:
    assert ids.verhoeff_valid(VALID_AADHAAR)
    assert ids.validate_aadhaar(VALID_AADHAAR) == VALID_AADHAAR


def test_one_mistyped_digit_fails_the_check_digit() -> None:
    wrong = VALID_AADHAAR[:5] + str((int(VALID_AADHAAR[5]) + 1) % 10) + VALID_AADHAAR[6:]
    with pytest.raises(ids.IdentifierError, match="check digit"):
        ids.validate_aadhaar(wrong)


def test_two_swapped_digits_fail_the_check_digit() -> None:
    swapped = VALID_AADHAAR[:3] + VALID_AADHAAR[4] + VALID_AADHAAR[3] + VALID_AADHAAR[5:]
    assert swapped != VALID_AADHAAR
    with pytest.raises(ids.IdentifierError):
        ids.validate_aadhaar(swapped)


def test_an_aadhaar_never_starts_with_zero_or_one() -> None:
    with pytest.raises(ids.IdentifierError, match="never starts"):
        ids.validate_aadhaar(_with_check_digit("12345678901"))


def test_only_the_last_four_aadhaar_digits_are_kept() -> None:
    assert ids.aadhaar_last4(VALID_AADHAAR) == VALID_AADHAAR[-4:]


# ── IFSC and account ───────────────────────────────────────────────────────


def test_an_ifsc_needs_the_reserved_zero() -> None:
    assert ids.validate_ifsc("hdfc0001234") == "HDFC0001234"
    with pytest.raises(ids.IdentifierError, match="reserved zero"):
        ids.validate_ifsc("HDFC1001234")
    with pytest.raises(ids.IdentifierError):
        ids.validate_ifsc("HDFC000123")


def test_an_account_number_is_nine_to_eighteen_digits() -> None:
    assert ids.validate_account_number("0012 3456 7890") == "001234567890"
    with pytest.raises(ids.IdentifierError):
        ids.validate_account_number("12345678")


# ── Masking ────────────────────────────────────────────────────────────────


def test_masking_keeps_only_the_last_four() -> None:
    assert ids.mask_tail("ABCPE1234F") == "XXXXXX234F"
    assert ids.mask_tail("101234567890") == "XXXXXXXX7890"
    assert ids.mask_tail(None) is None
    assert ids.mask_aadhaar("9012") == "XXXX XXXX 9012"
    assert ids.mask_account("7890") == "XXXXXX7890"


def test_a_statutory_proposal_is_masked_for_everyone_but_hr() -> None:
    stored = {"pan": "ABCPE1234F", "aadhaar_last4": "9012"}
    for_employee = _public(ProfileSection.STATUTORY, stored, hr=False)
    for_hr = _public(ProfileSection.STATUTORY, stored, hr=True)
    assert for_employee == {"pan": "XXXXXX234F", "aadhaar_last4": "XXXX XXXX 9012"}
    # HR sees the PAN in full, and Aadhaar is its last four for HR too.
    assert for_hr == {"pan": "ABCPE1234F", "aadhaar_last4": "XXXX XXXX 9012"}


def test_a_bank_proposal_never_returns_the_ciphertext_or_the_number() -> None:
    stored = {
        "account_number_encrypted": "enc:v1:abc",
        "account_number_last4": "7890",
        "ifsc": "HDFC0001234",
        "account_holder_name": "Anita Sharma",
        "bank_name": None,
    }
    shown = _public(ProfileSection.BANK_ACCOUNT, stored, hr=False)
    assert "account_number_encrypted" not in shown
    assert "account_number_last4" not in shown
    assert "account_number" not in shown
    assert shown["account_number_masked"] == "XXXXXX7890"


def test_hr_reads_a_bank_proposal_with_the_full_number() -> None:
    tenant_id = uuid.uuid4()
    stored = {
        "account_number_encrypted": encrypt_secret("001234567890", tenant_id),
        "account_number_last4": "7890",
        "ifsc": "HDFC0001234",
        "account_holder_name": "Anita Sharma",
        "bank_name": None,
    }
    for_hr = _public(ProfileSection.BANK_ACCOUNT, stored, hr=True, tenant_id=tenant_id)
    assert for_hr["account_number"] == "001234567890"
    assert "account_number_masked" not in for_hr
    assert "account_number_encrypted" not in for_hr
    for_employee = _public(ProfileSection.BANK_ACCOUNT, stored, hr=False, tenant_id=tenant_id)
    assert "account_number" not in for_employee
    assert for_employee["account_number_masked"] == "XXXXXX7890"


# ── Section contracts ──────────────────────────────────────────────────────

_WRITE = TypeAdapter(SectionWrite)


def test_statutory_writes_validate_every_identifier() -> None:
    with pytest.raises(ValidationError, match="fourth character"):
        StatutoryDetails(pan="ABCCE1234F")
    with pytest.raises(ValidationError, match="check digit"):
        StatutoryDetails(aadhaar=VALID_AADHAAR[:-1] + str((int(VALID_AADHAAR[-1]) + 1) % 10))
    with pytest.raises(ValidationError, match="at least one"):
        StatutoryDetails()


def test_a_stored_statutory_change_holds_only_the_aadhaar_last_four() -> None:
    change = _WRITE.validate_python(
        {"section": "statutory", "data": {"pan": "abcpe1234f", "aadhaar": VALID_AADHAAR}}
    )
    stored = _stored_form(change, tenant_id=None)  # type: ignore[arg-type]
    assert stored == {"pan": "ABCPE1234F", "aadhaar_last4": VALID_AADHAAR[-4:]}
    assert VALID_AADHAAR not in str(stored)


def test_nominee_shares_must_total_one_hundred_per_purpose() -> None:
    nominee = {"name": "Ravi", "relationship": "spouse", "purpose": "pf", "share_percent": "60"}
    with pytest.raises(ValidationError, match="PF 60%"):
        _WRITE.validate_python({"section": "nominees", "data": [nominee]})

    ok = _WRITE.validate_python(
        {
            "section": "nominees",
            "data": [
                nominee,
                {**nominee, "name": "Meera", "relationship": "child", "share_percent": "40"},
                {**nominee, "purpose": "gratuity", "share_percent": "100"},
            ],
        }
    )
    assert sum(n.share_percent for n in ok.data if n.purpose.value == "pf") == Decimal(100)


def test_an_address_takes_an_indian_state_code_and_a_pincode() -> None:
    good = {"line1": "12 MG Road", "city": "Bengaluru", "state_code": "ka", "pincode": "560001"}
    assert Addresses(current=good).current.state_code == "KA"
    with pytest.raises(ValidationError, match="state"):
        Addresses(current={**good, "state_code": "XX"})
    with pytest.raises(ValidationError):
        Addresses(current={**good, "pincode": "056000"})


def test_permanent_same_as_current_copies_the_address() -> None:
    good = {"line1": "12 MG Road", "city": "Bengaluru", "state_code": "KA", "pincode": "560001"}
    both = Addresses(current=good, permanent_same_as_current=True)
    assert both.permanent == both.current


def test_a_section_write_refuses_fields_it_does_not_define() -> None:
    with pytest.raises(ValidationError):
        _WRITE.validate_python(
            {"section": "personal", "data": {"blood_group": "O+", "pan": "ABCPE1234F"}}
        )


def test_the_sensitive_sections_are_exactly_the_ones_that_reach_money_or_a_return() -> None:
    assert {s.value for s in SENSITIVE_SECTIONS} == {
        "identity",
        "addresses",
        "nominees",
        "statutory",
        "bank_account",
    }
    assert not DIRECT_SECTIONS & SENSITIVE_SECTIONS
