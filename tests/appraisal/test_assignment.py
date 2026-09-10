"""Slice 18 gate: template assignment resolves for everyone, deterministically.

Two things this file is about:

- **Nobody falls through.** An employee who matches no rule gets no appraisal,
  and nobody notices until they ask why they were not reviewed.
- **Overlapping rules resolve the same way every time.** "All engineers" and
  "all L5s" both match an L5 engineer; which wins has to be a decision, not an
  accident of storage order.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.services.appraisal.assignment import (
    AssignmentError,
    EmployeeFacts,
    Rule,
    resolve_all,
    resolve_template,
    validate_rule_set,
)

AS_OF = date(2027, 3, 31)

DEFAULT = Rule("r-default", 100, "tpl-standard", {}, "Everyone else")
SENIOR = Rule("r-senior", 10, "tpl-senior", {"grades": ["L5", "L6"]}, "Senior grades")
ENGINEERING = Rule(
    "r-eng", 20, "tpl-eng", {"department_ids": ["dept-eng"]}, "Engineering"
)
NEW_JOINER = Rule(
    "r-new", 5, "tpl-short", {"max_tenure_months": 6}, "New joiners"
)


def employee(**overrides) -> EmployeeFacts:
    base = {
        "employee_id": "e1",
        "employee_code": "ACM001",
        "grade": "L3",
        "department_id": "dept-ops",
        "location_id": "loc-blr",
        "employment_type": "permanent",
        "date_of_joining": date(2020, 1, 1),
    }
    base.update(overrides)
    return EmployeeFacts(**base)


def resolve(facts=None, rules=None):
    return resolve_template(
        facts=facts or employee(), rules=rules or [DEFAULT], as_of=AS_OF
    )


# ═══════════════════════════════════════════════════════════════════════════
# Priority — the gate
# ═══════════════════════════════════════════════════════════════════════════


def test_the_highest_priority_matching_rule_wins() -> None:
    """The checklist gate.

    An L5 engineer matches both "senior grades" (priority 10) and
    "engineering" (priority 20). Ten wins, every time.
    """
    l5_engineer = employee(grade="L5", department_id="dept-eng")
    result = resolve(l5_engineer, [DEFAULT, ENGINEERING, SENIOR])

    assert result.template_id == "tpl-senior"
    assert result.matched_rule_id == "r-senior"
    assert "priority 10" in result.reason


def test_the_result_does_not_depend_on_the_order_rules_are_supplied() -> None:
    """Which is what makes it a decision rather than an accident."""
    l5_engineer = employee(grade="L5", department_id="dept-eng")
    orders = [
        [DEFAULT, ENGINEERING, SENIOR],
        [SENIOR, DEFAULT, ENGINEERING],
        [ENGINEERING, SENIOR, DEFAULT],
    ]
    outcomes = {resolve(l5_engineer, order).template_id for order in orders}
    assert outcomes == {"tpl-senior"}


def test_a_lower_priority_rule_applies_when_the_higher_one_does_not_match() -> None:
    l3_engineer = employee(grade="L3", department_id="dept-eng")
    result = resolve(l3_engineer, [DEFAULT, ENGINEERING, SENIOR])
    assert result.template_id == "tpl-eng"


def test_the_default_rule_catches_everyone_else() -> None:
    result = resolve(employee(), [DEFAULT, ENGINEERING, SENIOR])
    assert result.template_id == "tpl-standard"
    assert "default rule" in result.reason


# ═══════════════════════════════════════════════════════════════════════════
# Conditions
# ═══════════════════════════════════════════════════════════════════════════


def test_a_grade_condition_matches_exactly() -> None:
    assert resolve(employee(grade="L5"), [DEFAULT, SENIOR]).template_id == "tpl-senior"
    assert resolve(employee(grade="L4"), [DEFAULT, SENIOR]).template_id == "tpl-standard"


def test_a_grade_with_a_trailing_space_does_not_match_and_falls_to_default() -> None:
    """Exactly the case a default rule exists for. It is a data problem, but
    it must not mean the employee goes unreviewed."""
    result = resolve(employee(grade="L5 "), [DEFAULT, SENIOR])
    assert result.template_id == "tpl-standard"
    assert result.is_resolved is True


def test_a_tenure_band_matches_on_months_of_service() -> None:
    new = employee(date_of_joining=date(2027, 1, 1))
    old = employee(date_of_joining=date(2020, 1, 1))

    assert resolve(new, [DEFAULT, NEW_JOINER]).template_id == "tpl-short"
    assert resolve(old, [DEFAULT, NEW_JOINER]).template_id == "tpl-standard"


def test_an_employee_with_no_joining_date_fails_a_tenure_rule_gracefully() -> None:
    """It falls through to the default rather than raising."""
    result = resolve(employee(date_of_joining=None), [DEFAULT, NEW_JOINER])
    assert result.template_id == "tpl-standard"


def test_several_conditions_on_one_rule_must_all_hold() -> None:
    combined = Rule(
        "r-combined", 10, "tpl-combined",
        {"grades": ["L5"], "department_ids": ["dept-eng"]}, "Senior engineers",
    )
    both = employee(grade="L5", department_id="dept-eng")
    one = employee(grade="L5", department_id="dept-ops")

    assert resolve(both, [DEFAULT, combined]).template_id == "tpl-combined"
    assert resolve(one, [DEFAULT, combined]).template_id == "tpl-standard"


def test_an_employment_type_condition_matches() -> None:
    contractors = Rule(
        "r-contract", 10, "tpl-contract", {"employment_types": ["contract"]}, "Contract"
    )
    assert (
        resolve(employee(employment_type="contract"), [DEFAULT, contractors]).template_id
        == "tpl-contract"
    )


# ═══════════════════════════════════════════════════════════════════════════
# Nobody falls through — the gate
# ═══════════════════════════════════════════════════════════════════════════


def test_an_employee_matching_nothing_is_reported_with_what_they_missed() -> None:
    """A count alone would leave somebody guessing which employee and why."""
    result = resolve(employee(grade="L3"), [SENIOR, ENGINEERING])

    assert result.is_resolved is False
    assert result.template_id is None
    assert "ACM001 matches no rule" in result.reason
    assert "Senior grades" in result.reason
    assert "Add a default rule" in result.reason


def test_a_rule_set_with_no_default_is_refused_at_validation() -> None:
    """The checklist gate: refused at activation, when it is still a
    configuration problem rather than a people problem."""
    with pytest.raises(AssignmentError, match="No default template rule"):
        validate_rule_set([SENIOR, ENGINEERING])


def test_a_rule_set_with_a_default_validates() -> None:
    validate_rule_set([DEFAULT, SENIOR, ENGINEERING])


def test_an_empty_rule_set_is_refused() -> None:
    with pytest.raises(AssignmentError, match="at least one template assignment rule"):
        validate_rule_set([])


def test_duplicate_priorities_are_refused() -> None:
    """Two rules at the same priority means the winner depends on the order
    rows come back in, which is not a decision."""
    clashing = Rule("r-other", 10, "tpl-other", {"grades": ["L4"]}, "Also ten")
    with pytest.raises(AssignmentError, match="used more than once"):
        validate_rule_set([DEFAULT, SENIOR, clashing])


def test_an_unknown_condition_is_refused_rather_than_never_matching() -> None:
    """A rule that can never match looks exactly like a rule that is not
    needed, and nobody investigates the second one."""
    typo = Rule("r-typo", 10, "tpl-x", {"grade": ["L5"]}, "Typo'd condition")
    with pytest.raises(AssignmentError, match="does not understand"):
        validate_rule_set([DEFAULT, typo])


def test_the_unknown_condition_error_lists_what_is_available() -> None:
    typo = Rule("r-typo", 10, "tpl-x", {"seniority": ["high"]}, "Wrong key")
    with pytest.raises(AssignmentError, match="grades"):
        validate_rule_set([DEFAULT, typo])


# ═══════════════════════════════════════════════════════════════════════════
# Resolving a whole population
# ═══════════════════════════════════════════════════════════════════════════


def test_a_whole_population_resolves_and_reports_both_sides() -> None:
    population = [
        employee(employee_id="e1", employee_code="A", grade="L5"),
        employee(employee_id="e2", employee_code="B", department_id="dept-eng"),
        employee(employee_id="e3", employee_code="C"),
    ]
    resolved, unresolved = resolve_all(
        population=population, rules=[DEFAULT, SENIOR, ENGINEERING], as_of=AS_OF
    )

    assert len(resolved) == 3
    assert unresolved == []
    assert {r.template_id for r in resolved} == {"tpl-senior", "tpl-eng", "tpl-standard"}


def test_unresolved_employees_are_named_not_counted() -> None:
    population = [
        employee(employee_id="e1", employee_code="A", grade="L5"),
        employee(employee_id="e2", employee_code="B", grade="L3"),
    ]
    resolved, unresolved = resolve_all(
        population=population, rules=[SENIOR], as_of=AS_OF
    )

    assert len(resolved) == 1
    assert len(unresolved) == 1
    assert unresolved[0].employee_code == "B"
    assert "matches no rule" in unresolved[0].reason


def test_no_rules_at_all_resolves_nobody_and_says_so() -> None:
    result = resolve_template(facts=employee(), rules=[], as_of=AS_OF)
    assert result.is_resolved is False
    assert "No assignment rules are configured" in result.reason


def test_the_result_is_json_serialisable() -> None:
    import json

    json.dumps(resolve().as_dict())


def test_resolution_is_deterministic() -> None:
    facts = employee(grade="L5", department_id="dept-eng")
    rules = [DEFAULT, ENGINEERING, SENIOR]
    assert resolve(facts, rules).as_dict() == resolve(facts, rules).as_dict()
