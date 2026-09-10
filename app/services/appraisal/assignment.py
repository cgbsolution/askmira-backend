"""Template assignment: which template each employee is reviewed against.

═══════════════════════════════════════════════════════════════════════════════
NOBODY FALLS THROUGH
═══════════════════════════════════════════════════════════════════════════════

A rule engine with no default assigns nothing to the employee whose grade was
entered as "L3 " with a trailing space, or who joined last week and matches no
tenure band, or whose department was renamed. They get no template, no
appraisal, and nobody notices until they ask why they were not reviewed.

So a default template is **required**, and activation refuses if any eligible
employee cannot be resolved — checked at activation, when it is still a
configuration problem, rather than at launch when it is a people problem.

**First match wins, by priority.** Rules overlap constantly in practice ("all
engineers" and "all L5s" both match an L5 engineer), and the resolution has to
be the same every time it runs — not dependent on insertion order or on the
order a database happened to return rows in.
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

__all__ = [
    "AssignmentError",
    "AssignmentResult",
    "EmployeeFacts",
    "Rule",
    "resolve_template",
    "validate_rule_set",
]


class AssignmentError(ValueError):
    """A template cannot be resolved from the rules supplied."""


@dataclass(frozen=True, slots=True)
class EmployeeFacts:
    """What a rule can match on.

    Deliberately a fixed set. A condition language that can reach arbitrary
    employee fields becomes a scripting language nobody can validate at
    activation — and validating at activation is the whole point.
    """

    employee_id: str
    employee_code: str
    grade: str | None = None
    department_id: str | None = None
    location_id: str | None = None
    employment_type: str | None = None
    date_of_joining: date | None = None

    def tenure_months(self, as_of: date) -> int | None:
        if self.date_of_joining is None:
            return None
        return max(0, (as_of - self.date_of_joining).days // 30)


@dataclass(frozen=True, slots=True)
class Rule:
    """One assignment rule.

    `conditions` holds only the keys `EmployeeFacts` knows about. An unknown
    key is refused at validation rather than silently never matching — a rule
    that never fires looks identical to a rule that is not needed.
    """

    rule_id: str
    priority: int
    template_id: str
    conditions: dict[str, Any] = field(default_factory=dict)
    label: str = ""

    @property
    def is_default(self) -> bool:
        """A rule with no conditions matches everyone."""
        return not self.conditions


KNOWN_CONDITIONS = {
    "grades",
    "department_ids",
    "location_ids",
    "employment_types",
    "min_tenure_months",
    "max_tenure_months",
}


@dataclass(frozen=True, slots=True)
class AssignmentResult:
    employee_id: str
    employee_code: str
    template_id: str | None
    matched_rule_id: str | None
    reason: str

    @property
    def is_resolved(self) -> bool:
        return self.template_id is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "employee_id": self.employee_id,
            "employee_code": self.employee_code,
            "template_id": self.template_id,
            "matched_rule_id": self.matched_rule_id,
            "resolved": self.is_resolved,
            "reason": self.reason,
        }


def validate_rule_set(rules: list[Rule]) -> None:
    """Checked at activation, not at launch.

    At activation an unresolvable employee is a configuration problem somebody
    can fix in a minute. At launch it is an employee with no appraisal, and
    nobody finds out until they ask why they were not reviewed.
    """
    if not rules:
        raise AssignmentError(
            "A cycle needs at least one template assignment rule, and one of them has "
            "to be a default."
        )

    priorities = [r.priority for r in rules]
    duplicates = {p for p in priorities if priorities.count(p) > 1}
    if duplicates:
        raise AssignmentError(
            f"These priorities are used more than once: {sorted(duplicates)}. Rules "
            "overlap constantly — two rules at the same priority means the winner "
            "depends on the order rows come back in, which is not a decision."
        )

    for rule in rules:
        unknown = set(rule.conditions) - KNOWN_CONDITIONS
        if unknown:
            raise AssignmentError(
                f"Rule '{rule.label or rule.rule_id}' has conditions this engine does "
                f"not understand: {sorted(unknown)}. A rule that can never match looks "
                f"exactly like a rule that is not needed. Known conditions are: "
                f"{sorted(KNOWN_CONDITIONS)}."
            )

    if not any(r.is_default for r in rules):
        raise AssignmentError(
            "No default template rule. Without one, an employee whose grade was typed "
            "with a trailing space, or who matches no tenure band, gets no appraisal at "
            "all — and nobody notices until they ask why they were not reviewed."
        )


def _matches(rule: Rule, facts: EmployeeFacts, as_of: date) -> tuple[bool, str]:
    """Whether one rule applies, and which condition ruled it out."""
    conditions = rule.conditions

    grades = conditions.get("grades")
    if grades and facts.grade not in grades:
        return False, f"grade '{facts.grade}' is not in {grades}"

    departments = conditions.get("department_ids")
    if departments and str(facts.department_id) not in {str(d) for d in departments}:
        return False, "department is not in scope"

    locations = conditions.get("location_ids")
    if locations and str(facts.location_id) not in {str(loc) for loc in locations}:
        return False, "location is not in scope"

    types = conditions.get("employment_types")
    if types and facts.employment_type not in types:
        return False, f"employment type '{facts.employment_type}' is not in {types}"

    minimum = conditions.get("min_tenure_months")
    maximum = conditions.get("max_tenure_months")
    if minimum is not None or maximum is not None:
        tenure = facts.tenure_months(as_of)
        if tenure is None:
            return False, "no joining date, so tenure cannot be evaluated"
        if minimum is not None and tenure < int(minimum):
            return False, f"{tenure} months of tenure is below the minimum of {minimum}"
        if maximum is not None and tenure > int(maximum):
            return False, f"{tenure} months of tenure is above the maximum of {maximum}"

    return True, "matched"


def resolve_template(
    *, facts: EmployeeFacts, rules: list[Rule], as_of: date
) -> AssignmentResult:
    """The template this employee is reviewed against.

    Rules are evaluated in priority order, lowest number first, and the first
    match wins. Overlap is normal — "all engineers" and "all L5s" both match an
    L5 engineer — so the order has to be an explicit decision rather than an
    accident of storage.
    """
    if not rules:
        return AssignmentResult(
            employee_id=facts.employee_id,
            employee_code=facts.employee_code,
            template_id=None,
            matched_rule_id=None,
            reason="No assignment rules are configured for this cycle.",
        )

    ordered = sorted(rules, key=lambda r: (r.priority, r.rule_id))
    near_misses: list[str] = []

    for rule in ordered:
        matched, why = _matches(rule, facts, as_of)
        if matched:
            return AssignmentResult(
                employee_id=facts.employee_id,
                employee_code=facts.employee_code,
                template_id=rule.template_id,
                matched_rule_id=rule.rule_id,
                reason=(
                    f"Matched the default rule '{rule.label or rule.rule_id}'."
                    if rule.is_default
                    else f"Matched '{rule.label or rule.rule_id}' at priority {rule.priority}."
                ),
            )
        near_misses.append(f"'{rule.label or rule.rule_id}' ({why})")

    return AssignmentResult(
        employee_id=facts.employee_id,
        employee_code=facts.employee_code,
        template_id=None,
        matched_rule_id=None,
        reason=(
            f"{facts.employee_code} matches no rule. Tried: {'; '.join(near_misses)}. "
            "Add a default rule so nobody falls through."
        ),
    )


def resolve_all(
    *, population: list[EmployeeFacts], rules: list[Rule], as_of: date
) -> tuple[list[AssignmentResult], list[AssignmentResult]]:
    """Resolve everyone, returning `(resolved, unresolved)`.

    The unresolved list is what blocks activation, and it names each employee
    and what they failed to match — a count alone would leave somebody
    guessing.
    """
    results = [
        resolve_template(facts=facts, rules=rules, as_of=as_of) for facts in population
    ]
    return (
        [r for r in results if r.is_resolved],
        [r for r in results if not r.is_resolved],
    )
