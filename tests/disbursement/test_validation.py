"""Slice 11 gate: validation blocks rather than warns.

Every condition here blocks. That is a deliberate departure from the statutory
returns, which have a warning tier: a rejected return is an annoyance, but a
salary credit to a wrong account is money that has left the company and an
employee who has not been paid.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.disbursement.validation import (
    name_similarity,
    normalise_name,
    validate_ifsc,
    validate_lines,
)


def D(value: str | int) -> Decimal:  # noqa: N802 - a Decimal alias, used throughout
    return Decimal(str(value))


def row(**overrides) -> dict:
    base = {
        "subject": "ACM001",
        "employee_name": "Anita Sharma",
        "account_holder_name": "Anita Sharma",
        "account_number": "50100123456789",
        "ifsc": "HDFC0001234",
        "amount": D(48_200),
    }
    base.update(overrides)
    return base


def codes(result) -> set[str]:
    return {e.code for e in result.exceptions}


# ═══════════════════════════════════════════════════════════════════════════
# IFSC
# ═══════════════════════════════════════════════════════════════════════════


def test_a_well_formed_ifsc_passes() -> None:
    assert validate_ifsc("HDFC0001234") is None
    assert validate_ifsc("SBIN0000456") is None


def test_the_reserved_zero_is_checked_and_the_message_says_why() -> None:
    """The fifth character is the one people mistype from a cheque."""
    problem = validate_ifsc("HDFC1001234")
    assert problem is not None
    assert "reserved zero in the fifth position" in problem


def test_a_short_ifsc_is_refused_with_its_length() -> None:
    problem = validate_ifsc("HDFC001")
    assert problem is not None
    assert "7 characters" in problem


def test_a_missing_ifsc_is_refused() -> None:
    assert validate_ifsc(None) == "No IFSC on the account."
    assert validate_ifsc("") == "No IFSC on the account."


def test_lowercase_and_padded_input_is_accepted() -> None:
    assert validate_ifsc("  hdfc0001234 ") is None


def test_an_ifsc_with_the_wrong_bank_code_shape_is_refused() -> None:
    """Four letters, then the zero. Digits in the bank code are not an IFSC."""
    assert validate_ifsc("HD1C0001234") is not None


# ═══════════════════════════════════════════════════════════════════════════
# Name matching
# ═══════════════════════════════════════════════════════════════════════════


def test_titles_and_punctuation_are_ignored() -> None:
    assert normalise_name("Ms. Anita K. Sharma") == "anita k sharma"
    assert normalise_name("SHRI RAHUL IYER") == "rahul iyer"


def test_a_middle_initial_does_not_count_against_a_match() -> None:
    """"Anita Sharma" and "Anita K Sharma" are the same person.

    An edit distance would score them further apart than "Anita Sharma" and
    "Amita Sharma", which are not the same person at all — which is why this
    compares word tokens.
    """
    assert name_similarity("Anita Sharma", "Anita K Sharma") == D(1)


def test_an_unrelated_name_scores_low() -> None:
    assert name_similarity("Anita Sharma", "Rahul Iyer") == D(0)


def test_a_shared_surname_alone_is_a_partial_match() -> None:
    """Which is the case worth a human look, and the threshold decides."""
    assert name_similarity("Anita Sharma", "Rahul Sharma") == Decimal("0.50")


def test_a_reordered_name_still_matches() -> None:
    """Banks hold "Sharma Anita" as often as "Anita Sharma"."""
    assert name_similarity("Anita Sharma", "Sharma Anita") == D(1)


def test_an_empty_name_scores_zero_rather_than_raising() -> None:
    assert name_similarity("", "Anita Sharma") == D(0)


# ═══════════════════════════════════════════════════════════════════════════
# The batch
# ═══════════════════════════════════════════════════════════════════════════


def test_a_clean_batch_has_no_exceptions() -> None:
    assert validate_lines([row()]).is_blocked is False


def test_a_missing_account_number_blocks() -> None:
    result = validate_lines([row(account_number="")])
    assert result.is_blocked
    assert "ACCOUNT_MISSING" in codes(result)


def test_an_account_number_with_punctuation_blocks() -> None:
    result = validate_lines([row(account_number="5010-0123-456789")])
    assert "ACCOUNT_MALFORMED" in codes(result)


def test_a_too_short_account_number_blocks() -> None:
    result = validate_lines([row(account_number="12345")])
    assert "ACCOUNT_MALFORMED" in codes(result)


def test_a_leading_zero_account_number_is_accepted() -> None:
    """Several banks pad, which is why an account number is never an integer."""
    assert validate_lines([row(account_number="000123456789")]).is_blocked is False


def test_an_invalid_ifsc_blocks_generation() -> None:
    """The checklist gate."""
    result = validate_lines([row(ifsc="HDFC1001234")])
    assert result.is_blocked
    assert "IFSC_INVALID" in codes(result)


def test_zero_net_pay_blocks() -> None:
    result = validate_lines([row(amount=D(0))])
    assert "AMOUNT_NOT_POSITIVE" in codes(result)
    assert "leave them off the batch" in result.exceptions[0].message


def test_negative_net_pay_blocks() -> None:
    assert "AMOUNT_NOT_POSITIVE" in codes(validate_lines([row(amount=D(-1))]))


def test_a_float_amount_blocks() -> None:
    assert "AMOUNT_NOT_DECIMAL" in codes(validate_lines([row(amount=48200.0)]))


def test_a_name_mismatch_beyond_the_threshold_blocks() -> None:
    result = validate_lines([row(account_holder_name="Rahul Iyer")])
    assert "NAME_MISMATCH" in codes(result)
    assert "not recoverable by the company" in result.exceptions[0].message


def test_the_name_threshold_is_configuration() -> None:
    """A company that penny-drops every account can afford to be looser."""
    mismatched = [row(account_holder_name="Rahul Sharma")]  # similarity 0.50
    assert validate_lines(mismatched, name_match_threshold=D("0.4")).is_blocked is False
    assert validate_lines(mismatched, name_match_threshold=D("0.8")).is_blocked is True


def test_a_missing_holder_name_blocks() -> None:
    assert "HOLDER_NAME_MISSING" in codes(validate_lines([row(account_holder_name="")]))


def test_a_duplicate_account_number_is_flagged() -> None:
    """The checklist gate.

    Occasionally legitimate — a couple who bank together — so the message says
    so and the fix is to confirm the pair. Blocking is still right: far more
    often it pays one person twice and leaves another unpaid.
    """
    result = validate_lines([row(), row(subject="ACM002", employee_name="Rahul Sharma",
                                        account_holder_name="Anita Sharma")])
    assert result.is_blocked
    assert "ACCOUNT_DUPLICATE" in codes(result)

    duplicate = next(e for e in result.exceptions if e.code == "ACCOUNT_DUPLICATE")
    assert "ACM001" in duplicate.subject and "ACM002" in duplicate.subject
    assert "occasionally legitimate" in duplicate.message


def test_the_duplicate_message_shows_the_full_account_number() -> None:
    """Only admin and sub-admin see a batch's exceptions, and they see data
    unmasked (2026-10-27), so they can find the account without a lookup."""
    result = validate_lines([row(), row(subject="ACM002")])
    duplicate = next(e for e in result.exceptions if e.code == "ACCOUNT_DUPLICATE")
    assert "Account 50100123456789 appears on 2 employees" in duplicate.message


def test_two_different_accounts_are_not_flagged() -> None:
    result = validate_lines(
        [row(), row(subject="ACM002", account_number="50100999888777",
                    employee_name="Rahul Iyer", account_holder_name="Rahul Iyer")]
    )
    assert result.is_blocked is False


def test_every_exception_names_the_employee_and_the_field() -> None:
    """A worklist that says "validation failed" is not fixable."""
    result = validate_lines([row(ifsc="", account_number="", amount=D(0))])
    for exception in result.exceptions:
        assert exception.subject
        assert exception.field
        assert exception.message


def test_the_exception_list_is_json_serialisable() -> None:
    import json

    result = validate_lines([row(ifsc="bad")])
    assert json.loads(json.dumps(result.as_list()))


def test_the_summary_counts_the_exceptions() -> None:
    assert validate_lines([row()]).summary() == "No exceptions."
    assert "1 exception" in validate_lines([row(ifsc="bad")]).summary()


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("account_number", "", "ACCOUNT_MISSING"),
        ("ifsc", "", "IFSC_INVALID"),
        ("amount", Decimal(0), "AMOUNT_NOT_POSITIVE"),
        ("account_holder_name", "", "HOLDER_NAME_MISSING"),
    ],
)
def test_every_listed_condition_blocks_rather_than_warns(
    field: str, value: object, code: str
) -> None:
    """The checklist gate, across the whole list."""
    result = validate_lines([row(**{field: value})])
    assert result.is_blocked, f"{code} should block"
    assert code in codes(result)
