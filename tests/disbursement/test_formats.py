"""Slice 11 gate: the bank format serialisers.

Truncation at the boundary and paise-versus-rupees are the two that cost real
money if they are wrong — a name cut mid-word can fail a bank's own beneficiary
match and bounce the credit, and a rupee amount sent to a paise format
underpays by a factor of a hundred or is rejected outright.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.models.disbursement import BankFileFormat
from app.services.disbursement.formats import (
    FORMATS,
    FormatError,
    PaymentInstruction,
    PaymentLine,
    file_hash,
    serialise,
    truncate_name,
)


def D(value: str | int) -> Decimal:  # noqa: N802 - a Decimal alias, used throughout
    return Decimal(str(value))


def line(**overrides) -> PaymentLine:
    base = {
        "beneficiary_name": "Anita Sharma",
        "account_number": "50100123456789",
        "ifsc": "HDFC0001234",
        "amount": D("48200.00"),
        "employee_code": "ACM001",
    }
    base.update(overrides)
    return PaymentLine(**base)


def instruction(*lines, **overrides) -> PaymentInstruction:
    base = {
        "debit_account": "00110123456789",
        "debit_ifsc": "HDFC0000123",
        "corporate_id": "ACMECORP01",
        "value_date": date(2026, 6, 30),
        "batch_reference": "SAL202606",
        "lines": list(lines) or [line()],
    }
    base.update(overrides)
    return PaymentInstruction(**base)


# ═══════════════════════════════════════════════════════════════════════════
# Truncation at the boundary
# ═══════════════════════════════════════════════════════════════════════════


def test_a_name_within_the_limit_is_untouched() -> None:
    assert truncate_name("Anita Sharma", 40) == "Anita Sharma"


def test_a_name_exactly_at_the_limit_is_untouched() -> None:
    """The boundary case. Off by one here silently shortens every long name."""
    name = "A" * 40
    assert truncate_name(name, 40) == name
    assert len(truncate_name(name, 40)) == 40


def test_one_character_over_the_limit_truncates() -> None:
    assert len(truncate_name("A" * 41, 40)) == 40


def test_truncation_falls_back_to_a_word_boundary() -> None:
    """The checklist gate.

    A name cut mid-word can fail the bank's own beneficiary match and bounce
    the credit, so the cut is taken at the last space inside the limit.
    """
    name = "Anita Kumari Sharma Venkataraman"
    assert truncate_name(name, 20) == "Anita Kumari Sharma"
    assert " " not in truncate_name(name, 20)[-1]


def test_a_single_word_longer_than_the_limit_is_cut_hard() -> None:
    """There is no boundary to use, so a hard cut is all that is left."""
    assert truncate_name("Venkataramanujacharya", 10) == "Venkataram"


def test_a_cut_landing_exactly_on_a_word_end_keeps_that_word() -> None:
    """The bug this caught: backing off to the previous space when the cut was
    already on a boundary dropped a whole name.

    "Anita Kumari Sharma" at twelve characters is "Anita Kumari" — the space is
    at position twelve, so "Kumari" is complete. Returning "Anita" throws away
    a name the bank would have matched on.
    """
    assert truncate_name("Anita Kumari Sharma", 12) == "Anita Kumari"


def test_a_cut_landing_mid_word_backs_off_to_the_boundary() -> None:
    assert truncate_name("Anita Kumari Sharma", 15) == "Anita Kumari"


def test_extra_whitespace_is_collapsed_before_measuring() -> None:
    assert truncate_name("Anita    Sharma", 40) == "Anita Sharma"


def test_a_zero_length_limit_is_refused() -> None:
    with pytest.raises(FormatError, match="length limit of zero"):
        truncate_name("Anita", 0)


# ═══════════════════════════════════════════════════════════════════════════
# Amounts
# ═══════════════════════════════════════════════════════════════════════════


def test_a_rupee_format_carries_paise_as_decimals() -> None:
    output = serialise(instruction(), BankFileFormat.HDFC)
    assert "48200.00" in output


def test_a_paise_format_carries_whole_paise_as_an_integer() -> None:
    """The checklist gate.

    Sending "48200.00" to a paise format is a rejection; sending "48200" is a
    silent underpayment by a factor of a hundred.
    """
    output = serialise(instruction(), BankFileFormat.KOTAK)
    assert "4820000" in output
    assert "48200.00" not in output


def test_a_paise_format_rounds_rather_than_truncating() -> None:
    """Half a paise per employee, across a workforce, every month."""
    output = serialise(instruction(line(amount=D("48200.555"))), BankFileFormat.KOTAK)
    assert "4820056" in output


def test_a_float_amount_is_refused() -> None:
    with pytest.raises(FormatError, match="must be a Decimal"):
        serialise(instruction(line(amount=48200.0)), BankFileFormat.HDFC)


def test_a_zero_amount_is_refused_at_the_serialiser_too() -> None:
    """Validation catches it first; this is the second line of defence."""
    with pytest.raises(FormatError, match="would not be sent to a bank"):
        serialise(instruction(line(amount=D(0))), BankFileFormat.HDFC)


# ═══════════════════════════════════════════════════════════════════════════
# Per-format shape
# ═══════════════════════════════════════════════════════════════════════════


def test_the_hdfc_file_matches_the_golden_output() -> None:
    output = serialise(instruction(), BankFileFormat.HDFC)
    assert output == (
        "N,00110123456789,50100123456789,HDFC0001234,Anita Sharma,48200.00,N,"
        "30/06/2026,SALARY\r\n"
    )


def test_the_icici_file_carries_a_header_and_a_trailer() -> None:
    output = serialise(instruction(), BankFileFormat.ICICI)
    rows = output.strip().split("\r\n")
    assert rows[0].startswith("H|ACMECORP01|")
    assert rows[1].startswith("D|")
    assert rows[-1] == "T|1|48200.00"


def test_the_trailer_totals_every_line() -> None:
    """A trailer that disagrees with the body is how a bank rejects a file."""
    output = serialise(
        instruction(line(), line(amount=D("1800.00"), account_number="50100999888777")),
        BankFileFormat.ICICI,
    )
    assert output.strip().split("\r\n")[-1] == "T|2|50000.00"


def test_the_sbi_file_is_fixed_width_with_stable_columns() -> None:
    output = serialise(instruction(), BankFileFormat.SBI)
    rows = output.strip().split("\r\n")
    detail = rows[1]

    spec = FORMATS[BankFileFormat.SBI]
    assert len(detail) == sum(width for _, width in spec.widths)
    assert detail[:2] == "02"
    # The account number is right-justified with leading zeros in an 18 field.
    assert detail[2:20] == "000050100123456789"
    assert detail[20:31] == "HDFC0001234"


def test_an_over_width_field_is_refused_rather_than_shifting_the_file() -> None:
    """One long field puts every later field in the wrong place for the rest
    of the file, and the bank reads it as garbage."""
    with pytest.raises(FormatError, match="shifts every field after it"):
        serialise(instruction(line(narration="X" * 50)), BankFileFormat.SBI)


def test_every_format_produces_a_line_per_payment() -> None:
    payments = [
        line(),
        line(account_number="50100999888777"),
        line(account_number="50100111222333"),
    ]
    for bank_format in BankFileFormat:
        output = serialise(instruction(*payments), bank_format)
        spec = FORMATS[bank_format]
        expected = 3 + (1 if spec.has_header else 0) + (1 if spec.has_trailer else 0)
        assert len(output.strip().split("\r\n")) == expected, bank_format


def test_the_generic_neft_fallback_works() -> None:
    output = serialise(instruction(), BankFileFormat.GENERIC_NEFT)
    assert output.startswith("H,")
    assert "NEFT" in output


def test_a_delimiter_inside_a_field_is_refused() -> None:
    """Otherwise every column after it shifts and the bank misreads the row."""
    with pytest.raises(FormatError, match="contains ','"):
        serialise(instruction(line(beneficiary_name="Sharma, Anita")), BankFileFormat.HDFC)


def test_an_empty_file_is_refused() -> None:
    with pytest.raises(FormatError, match="no payment lines"):
        serialise(instruction(lines=[]), BankFileFormat.HDFC)


# ═══════════════════════════════════════════════════════════════════════════
# Per-corporate configuration
# ═══════════════════════════════════════════════════════════════════════════


def test_the_profile_config_overrides_the_name_length() -> None:
    """Banks set a different limit for one customer, so the spec is a default."""
    long_name = line(beneficiary_name="Anita Kumari Sharma Venkataraman")
    default = serialise(instruction(long_name), BankFileFormat.HDFC)
    narrow = serialise(
        instruction(long_name), BankFileFormat.HDFC, config={"name_length": 12}
    )
    assert "Anita Kumari Sharma Venkataraman" in default
    assert "Anita Kumari" in narrow
    assert "Venkataraman" not in narrow.split(",")[4]


def test_the_profile_config_overrides_the_transaction_code() -> None:
    output = serialise(
        instruction(), BankFileFormat.HDFC, config={"transaction_code": "RTGS"}
    )
    assert "RTGS" in output


def test_the_profile_config_overrides_the_date_format() -> None:
    output = serialise(
        instruction(), BankFileFormat.HDFC, config={"date_format": "%Y-%m-%d"}
    )
    assert "2026-06-30" in output


def test_an_unrecognised_config_key_is_ignored_rather_than_raising() -> None:
    """A bank adding a knob we do not read yet must not break a payment run."""
    assert serialise(instruction(), BankFileFormat.HDFC, config={"unknown": 1})


# ═══════════════════════════════════════════════════════════════════════════
# The hash
# ═══════════════════════════════════════════════════════════════════════════


def test_the_file_hash_changes_when_one_amount_changes() -> None:
    """It is what makes the approval name a specific file."""
    one = serialise(instruction(), BankFileFormat.HDFC)
    two = serialise(instruction(line(amount=D("48200.01"))), BankFileFormat.HDFC)
    assert file_hash(one) != file_hash(two)


def test_serialisation_is_deterministic() -> None:
    assert (
        serialise(instruction(), BankFileFormat.HDFC)
        == serialise(instruction(), BankFileFormat.HDFC)
    )
