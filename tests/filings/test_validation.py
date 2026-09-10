"""Slice 10 gate: validation blocks generation on known defects.

Every case here is something a portal would have rejected a day later, with a
row number and a code instead of an employee name. The point of the module is
that the list is fixable — so these tests assert on the exception's subject and
field, not only that something failed.
"""

from __future__ import annotations

from decimal import Decimal

from app.services.filings.validation import (
    Severity,
    validate_ecr_lines,
    validate_esic_lines,
    validate_pt_challan,
    validate_tds_return,
)


def D(value: str | int) -> Decimal:  # noqa: N802 - a Decimal alias, used throughout
    return Decimal(str(value))


def ecr_row(**overrides) -> dict:
    base = {
        "subject": "ACM001",
        "uan": "100123456789",
        "member_name": "ANITA SHARMA",
        "gross_wages": D(50_000),
        "epf_wages": D(15_000),
        "eps_wages": D(15_000),
        "edli_wages": D(15_000),
        "employee_share": D(1_800),
        "employer_epf_share": D(550),
        "eps_contribution": D(1_250),
        "ncp_days": D(0),
        "paid_days": D(30),
    }
    base.update(overrides)
    return base


def codes(result) -> set[str]:
    return {e.code for e in result.exceptions}


# ═══════════════════════════════════════════════════════════════════════════
# PF ECR
# ═══════════════════════════════════════════════════════════════════════════


def test_a_clean_ecr_produces_no_exceptions() -> None:
    result = validate_ecr_lines([ecr_row()])
    assert result.exceptions == []
    assert result.is_blocked is False


def test_a_blank_uan_blocks_generation() -> None:
    """The checklist gate. The portal rejects the whole file, not the row."""
    result = validate_ecr_lines([ecr_row(uan="")])
    assert result.is_blocked
    assert "UAN_MISSING" in codes(result)
    assert result.blocking[0].subject == "ACM001"
    assert result.blocking[0].field == "uan"


def test_a_malformed_uan_blocks_generation() -> None:
    result = validate_ecr_lines([ecr_row(uan="12345")])
    assert "UAN_MALFORMED" in codes(result)


def test_two_employees_sharing_a_uan_blocks_generation() -> None:
    """One of the two records is wrong, and the file would be nonsense."""
    result = validate_ecr_lines([ecr_row(), ecr_row(subject="ACM002")])
    assert "UAN_DUPLICATE" in codes(result)
    assert "ACM001" in result.blocking[0].message


def test_a_name_the_uan_database_cannot_hold_blocks_generation() -> None:
    """The most common ECR rejection, caught here instead of at the portal."""
    result = validate_ecr_lines([ecr_row(member_name="Anita Sharma-Iyer (Ms)")])
    assert "NAME_UNACCEPTABLE" in codes(result)


def test_an_ordinary_name_with_an_initial_is_accepted() -> None:
    result = validate_ecr_lines([ecr_row(member_name="ANITA K SHARMA")])
    assert result.is_blocked is False


def test_a_negative_value_blocks_generation() -> None:
    result = validate_ecr_lines([ecr_row(employee_share=D(-100))])
    assert "NEGATIVE_VALUE" in codes(result)
    assert result.blocking[0].field == "employee_share"


def test_a_float_blocks_generation() -> None:
    """Money in a statutory return is never a float."""
    result = validate_ecr_lines([ecr_row(epf_wages=15000.0)])
    assert "NOT_DECIMAL" in codes(result)


def test_the_normal_capped_case_raises_nothing_at_all() -> None:
    """Gross 50,000 with all three PF bases at a shared 15,000 ceiling.

    This is what most of an establishment looks like. An exception list that
    says something about every one of them is a list nobody reads, so identical
    wage bases are not flagged — the ceilings are not visible here, so the check
    could not tell this from genuine aliasing anyway.
    """
    result = validate_ecr_lines([ecr_row()])
    assert result.exceptions == []
    assert [e for e in result.exceptions if e.severity is Severity.WARNING] == []


def test_non_contributing_days_with_no_paid_days_blocks_generation() -> None:
    result = validate_ecr_lines([ecr_row(ncp_days=D(30), paid_days=D(0))])
    assert "NCP_INCONSISTENT" in codes(result)


def test_the_exception_list_is_json_serialisable() -> None:
    import json

    result = validate_ecr_lines([ecr_row(uan=""), ecr_row(subject="ACM002", member_name="")])
    payload = json.loads(json.dumps(result.as_list()))
    assert len(payload) == 2
    assert payload[0]["severity"] == "blocking"


def test_the_summary_counts_both_severities() -> None:
    result = validate_ecr_lines([ecr_row(uan="")])
    assert "1 blocking" in result.summary()
    assert validate_ecr_lines([ecr_row()]).summary() == "No exceptions."


# ═══════════════════════════════════════════════════════════════════════════
# ESIC
# ═══════════════════════════════════════════════════════════════════════════


def esic_row(**overrides) -> dict:
    base = {
        "subject": "ACM001",
        "ip_number": "3100123456",
        "days_worked": D(30),
        "wages_paid": D(18_000),
        "employee_contribution": D(135),
        "employer_contribution": D(585),
    }
    base.update(overrides)
    return base


def test_a_missing_insurance_number_blocks_generation() -> None:
    result = validate_esic_lines([esic_row(ip_number="")])
    assert "IP_MISSING" in codes(result)


def test_a_non_numeric_insurance_number_blocks_generation() -> None:
    result = validate_esic_lines([esic_row(ip_number="IP-31001")])
    assert "IP_MALFORMED" in codes(result)


def test_wages_against_zero_days_blocks_generation() -> None:
    result = validate_esic_lines([esic_row(days_worked=D(0), wages_paid=D(18_000))])
    assert "WAGES_WITHOUT_DAYS" in codes(result)


def test_days_without_wages_warns_rather_than_blocks() -> None:
    """Correct for unpaid leave that still counts as service."""
    result = validate_esic_lines([esic_row(wages_paid=D(0))])
    assert result.is_blocked is False
    assert "DAYS_WITHOUT_WAGES" in codes(result)


# ═══════════════════════════════════════════════════════════════════════════
# Professional Tax
# ═══════════════════════════════════════════════════════════════════════════


def test_a_missing_state_registration_blocks_the_challan() -> None:
    result = validate_pt_challan(state_code="KA", registration_number=None, rows=[])
    assert "PT_REGISTRATION_MISSING" in codes(result)
    assert result.blocking[0].subject == "KA"


def test_a_registered_state_with_clean_rows_passes() -> None:
    result = validate_pt_challan(
        state_code="KA",
        registration_number="PT/BLR/12345",
        rows=[{"subject": "ACM001", "tax_deducted": D(200)}],
    )
    assert result.is_blocked is False


# ═══════════════════════════════════════════════════════════════════════════
# TDS 24Q
# ═══════════════════════════════════════════════════════════════════════════


def deductee(**overrides) -> dict:
    base = {
        "subject": "ACM001",
        "pan": "ABCDE1234F",
        "tds_deducted": D(45_000),
        "tds_deposited": D(45_000),
    }
    base.update(overrides)
    return base


def test_a_clean_q1_return_passes() -> None:
    result = validate_tds_return(
        tan="BLRA12345B", quarter=1, deductees=[deductee()], unmapped_deductions=[]
    )
    assert result.is_blocked is False


def test_an_unmapped_deduction_blocks_generation() -> None:
    """The checklist gate: challan mapping is enforced before 24Q generation.

    A return with an unmapped deduction cannot be filed at all, and the portal's
    message about it will not name the employee.
    """
    result = validate_tds_return(
        tan="BLRA12345B",
        quarter=1,
        deductees=[],
        unmapped_deductions=[
            {"subject": "ACM001", "amount": "45000", "period": "06/2026"}
        ],
    )
    assert "CHALLAN_UNMAPPED" in codes(result)
    assert "ACM001" in result.blocking[0].subject


def test_a_missing_pan_blocks_generation() -> None:
    result = validate_tds_return(
        tan="T", quarter=1, deductees=[deductee(pan=None)], unmapped_deductions=[]
    )
    assert "PAN_MISSING" in codes(result)
    assert "higher rate" in result.blocking[0].message


def test_an_invalid_pan_blocks_generation() -> None:
    result = validate_tds_return(
        tan="T", quarter=1, deductees=[deductee(pan="ABCD1234EF")], unmapped_deductions=[]
    )
    assert "PAN_INVALID" in codes(result)


def test_a_lowercase_pan_is_accepted_after_normalising() -> None:
    result = validate_tds_return(
        tan="T", quarter=1, deductees=[deductee(pan="abcde1234f")], unmapped_deductions=[]
    )
    assert result.is_blocked is False


def test_two_employees_sharing_a_pan_blocks_generation() -> None:
    result = validate_tds_return(
        tan="T", quarter=1,
        deductees=[deductee(), deductee(subject="ACM002")],
        unmapped_deductions=[],
    )
    assert "PAN_DUPLICATE" in codes(result)


def test_deducted_and_deposited_must_agree() -> None:
    result = validate_tds_return(
        tan="T", quarter=1,
        deductees=[deductee(tds_deposited=D(40_000))],
        unmapped_deductions=[],
    )
    assert "TDS_MISMATCH" in codes(result)


def test_a_missing_tan_blocks_generation() -> None:
    result = validate_tds_return(
        tan=None, quarter=1, deductees=[deductee()], unmapped_deductions=[]
    )
    assert "TAN_MISSING" in codes(result)


def test_q4_without_annexure_ii_blocks_generation() -> None:
    result = validate_tds_return(
        tan="T", quarter=4, deductees=[deductee()], unmapped_deductions=[],
        salary_details=[],
    )
    assert "ANNEXURE_II_MISSING" in codes(result)


def test_q1_with_annexure_ii_blocks_generation() -> None:
    """Filing a year's figures before the year has happened."""
    result = validate_tds_return(
        tan="T", quarter=1, deductees=[deductee()], unmapped_deductions=[],
        salary_details=[{"pan": "ABCDE1234F"}],
    )
    assert "ANNEXURE_II_UNEXPECTED" in codes(result)


def test_q4_with_annexure_ii_passes() -> None:
    result = validate_tds_return(
        tan="T", quarter=4, deductees=[deductee()], unmapped_deductions=[],
        salary_details=[{"pan": "ABCDE1234F"}],
    )
    assert result.is_blocked is False
