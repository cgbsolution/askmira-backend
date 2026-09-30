"""The line manager's stage, and what a claim tells its claimant about the money."""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.models.expenses import (
    ExpenseClaim,
    ExpenseClaimStatusEnum,
    ExpenseReportStatusEnum,
)
from app.models.payroll import PayrollRun, PayrollRunStatus
from app.schemas.expenses import ClaimIn
from app.services.expenses.reads import payment_note
from app.services.expenses.service import derive_report_status, first_stage

S = ExpenseClaimStatusEnum


def test_a_claim_goes_to_the_line_manager_first() -> None:
    assert first_stage(True)[0] is S.PENDING_MANAGER


def test_with_no_line_manager_it_goes_straight_to_finance() -> None:
    stage, said = first_stage(False)
    assert stage is S.SUBMITTED
    assert "finance" in said


def test_a_report_waiting_on_its_manager_reads_as_submitted() -> None:
    assert derive_report_status([S.PENDING_MANAGER, S.PENDING_MANAGER]) is (
        ExpenseReportStatusEnum.SUBMITTED
    )
    assert derive_report_status([S.APPROVED, S.PENDING_MANAGER]) is (
        ExpenseReportStatusEnum.SUBMITTED
    )
    assert derive_report_status([S.REIMBURSED, S.APPROVED]) is ExpenseReportStatusEnum.APPROVED


def _claim(status: S, **fields) -> ExpenseClaim:
    return ExpenseClaim(id=uuid.uuid4(), status=status, **fields)


def _run(status: PayrollRunStatus) -> PayrollRun:
    return PayrollRun(id=uuid.uuid4(), period_year=2026, period_month=10, status=status)


def test_paid_is_said_only_of_a_run_that_was_paid() -> None:
    run = _run(PayrollRunStatus.PAID)
    period, status, note = payment_note(
        _claim(S.REIMBURSED, reimbursed_in_run_id=run.id), run
    )
    assert period == "October 2026" and status == "paid"
    assert note == "Paid in October 2026 payroll."

    approved = _run(PayrollRunStatus.APPROVED)
    _, _, pending = payment_note(_claim(S.REIMBURSED, reimbursed_in_run_id=approved.id), approved)
    assert pending.startswith("Included in the approved October 2026 payroll")


def test_an_advance_covered_claim_says_so() -> None:
    settled = uuid.uuid4()
    _, _, note = payment_note(_claim(S.REIMBURSED, advance_settlement_id=settled), None)
    assert "travel advance" in note
    _, _, balance = payment_note(_claim(S.APPROVED, advance_settlement_id=settled), None)
    assert "balance due to you is paid in your next payroll" in balance
    _, _, waiting = payment_note(_claim(S.APPROVED), None, open_advance=True)
    assert "settled against your travel advance" in waiting


def test_an_approved_claim_is_paid_in_the_next_payroll() -> None:
    assert payment_note(_claim(S.APPROVED), None)[2] == "Approved. It is paid in your next payroll."
    assert "line manager" in payment_note(_claim(S.PENDING_MANAGER), None)[2]
    assert "finance" in payment_note(_claim(S.SUBMITTED), None)[2]
    assert payment_note(_claim(S.DRAFT), None) == (None, None, None)


def test_an_expense_from_the_future_is_refused_at_the_door() -> None:
    body = {
        "category_key": "taxi",
        "city_tier": "a",
        "amount": Decimal("100"),
        "incurred_on": date.today() + timedelta(days=3),
    }
    with pytest.raises(ValidationError, match="before it is incurred"):
        ClaimIn(**body)
    body["incurred_on"] = date.today() - timedelta(days=1)
    assert ClaimIn(**body).incurred_on == body["incurred_on"]
