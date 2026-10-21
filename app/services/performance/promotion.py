"""Promotion as one operation, and letters that release together.

═══════════════════════════════════════════════════════════════════════════════
A PROMOTION THAT HALF-APPLIES IS A PAYROLL AND REPORTING MESS
═══════════════════════════════════════════════════════════════════════════════

A promotion is four things: a grade change, a salary revision, a position
change, and a letter. Apply three of them and the employee is on the new grade
at the old salary, or on the new salary in the old position, and it is found
either by the employee reading their payslip or by somebody reconciling
headcount two months later.

The transaction is the service's job. What this module contributes is the part
a transaction cannot fix: **refusing to start** when one of the four cannot be
completed. Rolling back cleanly is good; not having attempted a promotion with
no target grade configured is better, because the rollback is invisible and the
manager is told nothing.

═══════════════════════════════════════════════════════════════════════════════
AND LETTERS RELEASE TOGETHER, ON A DATE
═══════════════════════════════════════════════════════════════════════════════

Releasing each letter as its approval lands means the first person to hear is
whoever has the fastest approver, and the last person to hear spends a fortnight
watching colleagues get letters. That is a fairness problem and a rumour
problem, and both are avoidable by holding them.

`release_plan` therefore refuses to release before the controlled date, and
refuses to release *part* of a cycle - the whole point is that they go together.

Pure. `today` and `now` are always supplied.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

__all__ = [
    "PromotionError",
    "PromotionPlan",
    "PromotionStep",
    "ReleaseDecision",
    "arrears_window",
    "plan_promotion",
    "release_plan",
]


class PromotionError(ValueError):
    """A promotion operation this module refuses."""


def money(value: Decimal | int | str) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class PromotionStep(str, enum.Enum):
    """The four things a promotion is.

    All four or none. Three of them leaves the employee on the new grade at the
    old salary, or on the new salary in the old position.
    """

    GRADE_CHANGE = "grade_change"
    SALARY_REVISION = "salary_revision"
    POSITION_CHANGE = "position_change"
    LETTER = "letter"


@dataclass(frozen=True, slots=True)
class PromotionPlan:
    """What will be applied, checked before anything is."""

    employee_id: str
    from_grade: str
    to_grade: str
    from_position_id: str
    to_position_id: str
    current_ctc: Decimal
    new_ctc: Decimal
    effective_from: date
    steps: tuple[PromotionStep, ...]
    is_backdated: bool
    arrears_from: date | None
    message: str

    @property
    def is_complete(self) -> bool:
        return set(self.steps) == set(PromotionStep)

    def as_dict(self) -> dict[str, Any]:
        return {
            "employee_id": self.employee_id,
            "from_grade": self.from_grade,
            "to_grade": self.to_grade,
            "from_position_id": self.from_position_id,
            "to_position_id": self.to_position_id,
            "current_ctc": str(self.current_ctc),
            "new_ctc": str(self.new_ctc),
            "effective_from": self.effective_from.isoformat(),
            "steps": [s.value for s in self.steps],
            "is_complete": self.is_complete,
            "is_backdated": self.is_backdated,
            "arrears_from": (self.arrears_from.isoformat() if self.arrears_from else None),
            "message": self.message,
        }


def plan_promotion(
    *,
    employee_id: str,
    from_grade: str,
    to_grade: str,
    from_position_id: str,
    to_position_id: str,
    current_ctc: Decimal,
    new_ctc: Decimal,
    effective_from: date,
    today: date,
) -> PromotionPlan:
    """Assemble the four steps, refusing before starting rather than rolling back.

    Every refusal here is a thing a transaction could not have saved: a missing
    target grade, a promotion to the same grade, a promotion that reduces pay.
    Rolling those back cleanly leaves the manager with nothing on screen and no
    idea what happened.
    """
    if not to_grade.strip():
        raise PromotionError(
            "A promotion needs a target grade. Applying the salary and the "
            "position without it leaves the employee promoted everywhere except "
            "in the grade that drives their band, their leave entitlement and "
            "their next increment."
        )
    if to_grade == from_grade:
        raise PromotionError(
            f"The target grade {to_grade!r} is the current one. A salary "
            "revision without a grade change is an increment, and calling it a "
            "promotion misstates the employee's record and the headcount plan."
        )
    if not to_position_id.strip():
        raise PromotionError(
            "A promotion needs a target position. Without it the reporting line "
            "and the headcount budget still describe the old role, and the gap "
            "is found by whoever reconciles establishment next quarter."
        )
    if new_ctc < current_ctc:
        raise PromotionError(
            f"The new CTC {money(new_ctc)} is below the current "
            f"{money(current_ctc)}. A promotion that reduces pay may be a real "
            "arrangement, but it is not one this path should apply silently."
        )

    backdated = effective_from < today
    return PromotionPlan(
        employee_id=employee_id,
        from_grade=from_grade,
        to_grade=to_grade,
        from_position_id=from_position_id,
        to_position_id=to_position_id,
        current_ctc=money(current_ctc),
        new_ctc=money(new_ctc),
        effective_from=effective_from,
        steps=tuple(PromotionStep),
        is_backdated=backdated,
        arrears_from=effective_from if backdated else None,
        message=(
            f"Promotion from {from_grade} to {to_grade}, "
            f"{money(current_ctc)} to {money(new_ctc)}, effective "
            f"{effective_from.isoformat()}. All four steps apply together."
            + (
                f" Back-dated: arrears are due from {effective_from.isoformat()}."
                if backdated
                else ""
            )
        ),
    )


def arrears_window(
    *, effective_from: date, today: date, last_payroll_run: date | None = None
) -> dict[str, Any]:
    """Whether arrears are due, and from when.

    Measured from the **effective date**, not from the last payroll run: a
    revision effective in April and approved in July owes April, May and June
    whether or not payroll happened to run in between. Anchoring on the payroll
    run is a plausible-looking bug that silently shortens the arrears period.
    """
    if effective_from >= today:
        return {
            "arrears_due": False,
            "from": None,
            "note": "Effective from today or later; nothing to back-pay.",
        }
    return {
        "arrears_due": True,
        "from": effective_from.isoformat(),
        "to": today.isoformat(),
        "months_spanned": (
            (today.year - effective_from.year) * 12 + today.month - effective_from.month
        ),
        "last_payroll_run": (last_payroll_run.isoformat() if last_payroll_run else None),
        "note": (
            "Arrears run from the effective date, not from the last payroll "
            "run. A revision effective in April and approved in July owes April "
            "onward whether or not payroll ran in between."
        ),
    }


# ══════════════════════════════════════════════════════════════════════════════
# Letter release
# ══════════════════════════════════════════════════════════════════════════════


class ReleaseBlock(str, enum.Enum):
    NONE = "none"
    BEFORE_RELEASE_DATE = "before_release_date"
    APPROVALS_OUTSTANDING = "approvals_outstanding"
    NOTHING_TO_RELEASE = "nothing_to_release"


@dataclass(frozen=True, slots=True)
class ReleaseDecision:
    """Whether this cycle's letters may go out.

    All of them or none. Releasing as each approval lands means the first person
    to hear is whoever has the fastest approver, and the last spends a fortnight
    watching colleagues get letters.
    """

    permitted: bool
    block: ReleaseBlock
    letter_count: int
    outstanding_approvals: tuple[str, ...]
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "permitted": self.permitted,
            "block": self.block.value,
            "letter_count": self.letter_count,
            "outstanding_approvals": list(self.outstanding_approvals),
            "message": self.message,
        }


def release_plan(
    *,
    release_on: date,
    today: date,
    approved_employee_ids: list[str],
    pending_employee_ids: list[str],
    force_partial: bool = False,
) -> ReleaseDecision:
    """Decide whether the whole cycle's letters go out today.

    Refuses before the controlled date and refuses while approvals are
    outstanding - the second is the one that matters, because releasing the
    approved ones "for now" is exactly the piecemeal release the controlled date
    exists to prevent.

    `force_partial` exists because a cycle occasionally has one genuinely stuck
    approval and holding two hundred letters for it helps nobody. It is a
    deliberate act with the outstanding names recorded, not a default.
    """
    if today < release_on:
        return ReleaseDecision(
            permitted=False,
            block=ReleaseBlock.BEFORE_RELEASE_DATE,
            letter_count=len(approved_employee_ids),
            outstanding_approvals=tuple(sorted(pending_employee_ids)),
            message=(
                f"Letters release on {release_on.isoformat()}. Holding them "
                "until then is the point: released as each approval lands, the "
                "first person to hear is whoever has the fastest approver."
            ),
        )

    if not approved_employee_ids:
        return ReleaseDecision(
            permitted=False,
            block=ReleaseBlock.NOTHING_TO_RELEASE,
            letter_count=0,
            outstanding_approvals=tuple(sorted(pending_employee_ids)),
            message="No approved recommendations in this cycle to release.",
        )

    if pending_employee_ids and not force_partial:
        return ReleaseDecision(
            permitted=False,
            block=ReleaseBlock.APPROVALS_OUTSTANDING,
            letter_count=len(approved_employee_ids),
            outstanding_approvals=tuple(sorted(pending_employee_ids)),
            message=(
                f"{len(pending_employee_ids)} recommendation(s) are still "
                "awaiting approval. Releasing the approved ones now is the "
                "piecemeal release the controlled date exists to prevent - "
                "finish the approvals, or release partially as a deliberate act "
                "with the outstanding names on the record."
            ),
        )

    return ReleaseDecision(
        permitted=True,
        block=ReleaseBlock.NONE,
        letter_count=len(approved_employee_ids),
        outstanding_approvals=tuple(sorted(pending_employee_ids)),
        message=(
            f"{len(approved_employee_ids)} letter(s) release together."
            + (
                f" **Partial release**: {len(pending_employee_ids)} "
                "recommendation(s) remain unapproved and are named on the "
                "record."
                if pending_employee_ids
                else ""
            )
        ),
    )
