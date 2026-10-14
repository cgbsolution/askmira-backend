"""Which approval rule applies to a request. Pure."""

from __future__ import annotations

import uuid
from decimal import Decimal

from app.models.approval_rules import ApprovalProcess, ApprovalRule
from app.services.approvals.rules import Facts, matches

SALES = uuid.uuid4()
PUNE = uuid.uuid4()


def _rule(**kw) -> ApprovalRule:
    base = {
        "process": ApprovalProcess.EXPENSE, "name": "r", "priority": 100, "active": True,
        "leave_type_ids": [], "expense_categories": [], "department_ids": [],
        "location_ids": [], "steps": [{"kind": "line_manager"}],
    }
    base.update(kw)
    return ApprovalRule(**base)


def test_a_rule_with_no_conditions_matches_its_process_only() -> None:
    rule = _rule()
    assert matches(rule, Facts(process=ApprovalProcess.EXPENSE))
    assert not matches(rule, Facts(process=ApprovalProcess.LEAVE))


def test_amount_bounds_are_inclusive() -> None:
    rule = _rule(min_amount=Decimal("50000"), max_amount=Decimal("200000"))
    assert matches(rule, Facts(ApprovalProcess.EXPENSE, amount=Decimal("50000")))
    assert matches(rule, Facts(ApprovalProcess.EXPENSE, amount=Decimal("200000")))
    assert not matches(rule, Facts(ApprovalProcess.EXPENSE, amount=Decimal("49999.99")))
    assert not matches(rule, Facts(ApprovalProcess.EXPENSE, amount=None))


def test_department_location_and_category_must_all_hold() -> None:
    rule = _rule(department_ids=[str(SALES)], location_ids=[str(PUNE)],
                 expense_categories=["hotel"])
    hit = Facts(ApprovalProcess.EXPENSE, department_id=SALES, location_id=PUNE, category="hotel")
    assert matches(rule, hit)
    assert not matches(rule, Facts(ApprovalProcess.EXPENSE, department_id=SALES,
                                   location_id=PUNE, category="meals"))
    assert not matches(rule, Facts(ApprovalProcess.EXPENSE, department_id=uuid.uuid4(),
                                   location_id=PUNE, category="hotel"))


def test_long_leave_needs_the_minimum_days() -> None:
    rule = _rule(process=ApprovalProcess.LEAVE, min_days=Decimal("5"))
    assert matches(rule, Facts(ApprovalProcess.LEAVE, days=Decimal("5")))
    assert not matches(rule, Facts(ApprovalProcess.LEAVE, days=Decimal("4.5")))


def test_an_inactive_rule_never_matches() -> None:
    assert not matches(_rule(active=False), Facts(ApprovalProcess.EXPENSE))
