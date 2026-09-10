"""The DB-facing half of recruitment analytics and referrals.

The funnel arithmetic, the cost components, the interviewer signals, the
referral rules and the diversity suppression all live in the pure modules beside
this. An invariant checks the isolation.

═══════════════════════════════════════════════════════════════════════════════
THE FUNNEL IS DERIVED, EVERY TIME
═══════════════════════════════════════════════════════════════════════════════

There is no summary table and no cached count. A materialised funnel is written
once and the underlying records move afterwards, and the first recruiter who
cross-checks it stops believing the dashboard. `funnel_report` reads
applications and rebuilds the journeys on every call, and returns the
reconciliation beside the chart so a discrepancy is visible rather than
rendered.

═══════════════════════════════════════════════════════════════════════════════
THE DUPLICATE CHECK RUNS BEFORE THE REFERRAL IS ACCEPTED
═══════════════════════════════════════════════════════════════════════════════

Not afterwards in a nightly job. An employee who submits a referral and is told
three days later that a colleague got there first has already told the candidate
they referred them.

**What is and is not wired**, stated once here:

- A payout row is written when the candidate joins, with `paid_at` null.
  **Nothing in payroll picks it up**, so an owed milestone stays visible.
- Costs are recorded by a person. **No accounting system is read**, so a
  component with no row is missing rather than zero and the cost per hire says
  so.
- Share links are generated. **Nothing posts to WhatsApp or LinkedIn** — the
  employee's own app does that with the link.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.employee import Employee, EmploymentStatus
from app.models.recruiting import (
    CandidateDiversityDisclosure,
    CostComponentEnum,
    PayoutStatusEnum,
    RecruitmentCost,
    Referral,
    ReferralPayout,
    ReferralProgramme,
    ReferralStatusEnum,
    ShareChannelEnum,
)
from app.models.user import User
from app.services import audit
from app.services.recruiting import diversity as diversity_engine
from app.services.recruiting import funnel as funnel_engine
from app.services.recruiting import interviewers as interviewer_engine
from app.services.recruiting import referrals as referral_engine

__all__ = [
    "cost_report",
    "create_programme",
    "diversity_report",
    "generate_share_link",
    "interviewer_signals",
    "mark_referral_hired",
    "pay_milestone",
    "record_cost",
    "recruiting_posture",
    "submit_referral",
]


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def _not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def _d(value: Any) -> Decimal:
    return Decimal(str(value if value is not None else 0))


def normalise_candidate_key(raw: str) -> str:
    """The key duplicates are decided on.

    Case-folded with whitespace stripped. An email is the usual key and
    `Ravi.Kumar@X.com` and `ravi.kumar@x.com ` are the same person - matching
    the raw strings would let the same candidate be referred twice and both
    bonuses paid.
    """
    return re.sub(r"\s+", "", (raw or "").strip().lower())


# ══════════════════════════════════════════════════════════════════════════════
# Programme
# ══════════════════════════════════════════════════════════════════════════════


async def create_programme(
    session: AsyncSession,
    user: User,
    *,
    name: str,
    bonus_by_grade: dict[str, Decimal],
    milestones: list[dict[str, Any]] | None = None,
    eligible_roles: list[str] | None = None,
    description: str | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    """Create a programme and return its terms.

    The terms come back in the same response, because they are what an employee
    reads before referring - including the duplicate rule, which is the whole
    point of stating it up front.
    """
    specs = milestones or [
        {
            "key": m.key,
            "label": m.label,
            "share_percent": str(m.share_percent),
            "days_after_joining": m.days_after_joining,
        }
        for m in referral_engine.DEFAULT_MILESTONES
    ]
    engine_milestones = tuple(
        referral_engine.BonusMilestone(
            key=str(m["key"]),
            label=str(m["label"]),
            share_percent=_d(m["share_percent"]),
            days_after_joining=int(m["days_after_joining"]),
        )
        for m in specs
    )
    try:
        terms = referral_engine.programme_terms(
            name=name,
            bonus_by_grade={k: _d(v) for k, v in bonus_by_grade.items()},
            milestones=engine_milestones,
            eligible_roles=tuple(eligible_roles or []),
        )
    except referral_engine.ReferralError as exc:
        raise _bad_request(str(exc)) from exc

    row = ReferralProgramme(
        tenant_id=user.tenant_id,
        name=name,
        description=description,
        eligible_roles=list(eligible_roles or []),
        bonus_by_grade={k: str(_d(v)) for k, v in bonus_by_grade.items()},
        payout_milestones=specs,
    )
    session.add(row)
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="referral_programme",
        entity_id=row.id,
        actor=user,
        summary=f"Referral programme {name!r} created.",
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return {"programme_id": str(row.id), "terms": terms}


async def generate_share_link(
    session: AsyncSession,
    user: User,
    *,
    programme_id: uuid.UUID,
    requisition_id: uuid.UUID,
    referrer_employee_id: uuid.UUID,
    channel: ShareChannelEnum,
    base_url: str,
) -> dict[str, Any]:
    """A tracked link, with the programme terms beside it.

    The terms travel with the link because this is the moment the employee is
    deciding to share - a duplicate rule they read after referring is a rule
    that produces a grievance.
    """
    programme = (
        await session.execute(
            tenant_scoped(
                select(ReferralProgramme).where(
                    ReferralProgramme.id == programme_id
                ),
                ReferralProgramme,
                user,
            )
        )
    ).scalar_one_or_none()
    if programme is None:
        raise _not_found("No such referral programme in this tenant.")

    try:
        link = referral_engine.share_link(
            base_url=base_url,
            requisition_id=str(requisition_id),
            referrer_id=str(referrer_employee_id),
            channel=channel.value,
        )
    except referral_engine.ReferralError as exc:
        raise _bad_request(str(exc)) from exc

    return {
        "link": link,
        "terms": {
            "duplicate_rule": (
                "If two people refer the same candidate, the first submission "
                "wins by timestamp."
            ),
            "bonus_by_grade": programme.bonus_by_grade,
            "milestones": programme.payout_milestones,
            "tax_note": (
                "A referral bonus is salary income and is paid through payroll "
                "with tax deducted. The amounts above are gross."
            ),
        },
    }


# ══════════════════════════════════════════════════════════════════════════════
# Referral
# ══════════════════════════════════════════════════════════════════════════════


async def submit_referral(
    session: AsyncSession,
    user: User,
    *,
    programme_id: uuid.UUID,
    referrer_employee_id: uuid.UUID,
    candidate_key: str,
    requisition_id: uuid.UUID | None = None,
    candidate_id: uuid.UUID | None = None,
    role_key: str = "",
    on_pip: bool = False,
    hiring_team_requisitions: list[str] | None = None,
    channel: ShareChannelEnum | None = None,
    submitted_at: datetime | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    """Accept a referral, checking eligibility and duplicates before it lands.

    The duplicate check runs **now**, not in a nightly job. An employee told
    three days later that a colleague got there first has already told the
    candidate they referred them.
    """
    programme = (
        await session.execute(
            tenant_scoped(
                select(ReferralProgramme)
                .where(ReferralProgramme.id == programme_id)
                .where(ReferralProgramme.is_active.is_(True)),
                ReferralProgramme,
                user,
            )
        )
    ).scalar_one_or_none()
    if programme is None:
        raise _not_found("No such active referral programme in this tenant.")

    referrer = (
        await session.execute(
            tenant_scoped(
                select(Employee).where(Employee.id == referrer_employee_id),
                Employee,
                user,
            )
        )
    ).scalar_one_or_none()
    if referrer is None:
        raise _not_found("No such employee in this tenant.")

    # "Confirmed" means the probation window has closed. `probation_end_date`
    # in the past is the fact the employee record actually carries; a nullable
    # date with no value means no probation was set, which is confirmed.
    probation_over = (
        referrer.probation_end_date is None
        or referrer.probation_end_date <= (submitted_at or datetime.now(UTC)).date()
    )
    check = referral_engine.check_eligibility(
        referral_engine.ReferrerFacts(
            employee_id=str(referrer.id),
            is_confirmed=not programme.require_confirmed or probation_over,
            is_on_pip=on_pip and programme.exclude_on_pip,
            is_active=referrer.status is EmploymentStatus.ACTIVE,
            hiring_team_requisitions=tuple(hiring_team_requisitions or []),
        ),
        requisition_id=str(requisition_id or ""),
        eligible_roles=tuple(programme.eligible_roles or []),
        role_key=role_key,
    )
    if not check.eligible:
        raise _bad_request(check.message)

    key = normalise_candidate_key(candidate_key)
    if not key:
        raise _bad_request(
            "A referral needs something to identify the candidate by - normally "
            "their email. Without it two referrals of the same person cannot be "
            "recognised as duplicates, and both bonuses get paid."
        )

    moment = submitted_at or datetime.now(UTC)
    existing = list(
        (
            await session.execute(
                tenant_scoped(
                    select(Referral).where(Referral.candidate_key == key),
                    Referral,
                    user,
                )
            )
        ).scalars()
    )
    incoming = referral_engine.ReferralFacts(
        referral_id="pending",
        referrer_id=str(referrer_employee_id),
        candidate_key=key,
        submitted_at=moment,
    )
    verdict = referral_engine.resolve_duplicate(
        [
            referral_engine.ReferralFacts(
                referral_id=str(r.id),
                referrer_id=str(r.referrer_employee_id),
                candidate_key=r.candidate_key,
                submitted_at=r.submitted_at,
                status=referral_engine.ReferralStatus(r.status.value),
            )
            for r in existing
        ],
        incoming=incoming,
    )

    row = Referral(
        tenant_id=user.tenant_id,
        programme_id=programme_id,
        referrer_employee_id=referrer_employee_id,
        requisition_id=requisition_id,
        candidate_id=candidate_id,
        candidate_key=key,
        share_channel=channel,
        submitted_at=moment,
        status=(
            ReferralStatusEnum.DUPLICATE
            if verdict["is_duplicate"]
            else ReferralStatusEnum.SUBMITTED
        ),
        duplicate_of_id=(
            uuid.UUID(verdict["winner_referral_id"])
            if verdict["is_duplicate"]
            else None
        ),
    )
    session.add(row)

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="referral",
        entity_id=row.id,
        actor=user,
        summary=(
            "Referral submitted"
            + (" (duplicate)." if verdict["is_duplicate"] else ".")
        ),
        request=request,
    )
    await session.commit()
    await session.refresh(row)

    return {
        "referral_id": str(row.id),
        "status": row.status.value,
        "duplicate": verdict,
        "eligibility": check.as_dict(),
    }


async def mark_referral_hired(
    session: AsyncSession,
    user: User,
    *,
    referral_id: uuid.UUID,
    hired_on: date,
    grade: str,
    request: Request | None = None,
) -> dict[str, Any]:
    """Record the hire and write the whole payout schedule up front.

    Every milestone row is written now, with `paid_at` null. The ninety-day
    instalment is visible as owed from day one rather than appearing when
    somebody remembers to create it - which is how a referral bonus gets
    half-paid and nobody notices.
    """
    row = (
        await session.execute(
            tenant_scoped(
                select(Referral).where(Referral.id == referral_id), Referral, user
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise _not_found("No such referral in this tenant.")
    if row.status is ReferralStatusEnum.DUPLICATE:
        raise _bad_request(
            "This referral lost the duplicate race, so its bonus belongs to the "
            "colleague who referred first. Paying both would pay twice for one "
            "hire, and the rule was in the terms before either of them referred."
        )
    if row.hired_on is not None:
        raise _bad_request(
            "This referral is already marked hired. Marking it again would "
            "write a second payout schedule."
        )

    programme = (
        await session.execute(
            tenant_scoped(
                select(ReferralProgramme).where(
                    ReferralProgramme.id == row.programme_id
                ),
                ReferralProgramme,
                user,
            )
        )
    ).scalar_one()
    bonus = _d((programme.bonus_by_grade or {}).get(grade, 0))
    if bonus <= 0:
        raise _bad_request(
            f"This programme has no bonus configured for grade {grade!r}. "
            "Paying zero is not the same as having no rule, and the referrer "
            f"needs an answer. Configured: {sorted(programme.bonus_by_grade)}."
        )

    milestones = tuple(
        referral_engine.BonusMilestone(
            key=str(m["key"]),
            label=str(m["label"]),
            share_percent=_d(m["share_percent"]),
            days_after_joining=int(m["days_after_joining"]),
        )
        for m in (programme.payout_milestones or [])
    ) or referral_engine.DEFAULT_MILESTONES

    try:
        schedule = referral_engine.payout_schedule(
            bonus_amount=bonus, joined_on=hired_on, milestones=milestones
        )
    except referral_engine.ReferralError as exc:
        raise _bad_request(str(exc)) from exc

    row.status = ReferralStatusEnum.HIRED
    row.hired_on = hired_on
    row.bonus_amount = bonus

    for entry in schedule["milestones"]:
        session.add(
            ReferralPayout(
                tenant_id=user.tenant_id,
                referral_id=row.id,
                milestone_key=entry["key"],
                milestone_label=entry["label"],
                amount=Decimal(entry["amount"]),
                payable_on=date.fromisoformat(entry["payable_on"]),
                status=PayoutStatusEnum.SCHEDULED,
                payroll_reference=None,
            )
        )

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="referral",
        entity_id=row.id,
        actor=user,
        summary=(
            f"Referral hired {hired_on.isoformat()}; {bonus} scheduled across "
            f"{len(schedule['milestones'])} milestone(s)."
        ),
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return {"referral_id": str(row.id), "schedule": schedule}


async def pay_milestone(
    session: AsyncSession,
    user: User,
    *,
    payout_id: uuid.UUID,
    payroll_reference: str,
    request: Request | None = None,
) -> dict[str, Any]:
    """Mark one milestone as taken by payroll.

    Requires the payroll reference. Marking it paid without one records a
    payment nobody can trace, and the referrer asking where their money is
    cannot be answered.
    """
    if not payroll_reference.strip():
        raise _bad_request(
            "A payout needs the payroll reference it went out on. Marking it "
            "paid without one records a payment nobody can trace, and the "
            "referrer asking where their money is cannot be answered."
        )

    row = (
        await session.execute(
            tenant_scoped(
                select(ReferralPayout).where(ReferralPayout.id == payout_id),
                ReferralPayout,
                user,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise _not_found("No such referral payout in this tenant.")
    if row.status is not PayoutStatusEnum.SCHEDULED:
        raise _bad_request(f"This payout is already {row.status.value}.")

    row.status = PayoutStatusEnum.PAID
    row.payroll_reference = payroll_reference
    row.paid_at = datetime.now(UTC)

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="referral_payout",
        entity_id=row.id,
        actor=user,
        summary=(
            f"Referral milestone {row.milestone_key} paid via "
            f"{payroll_reference}."
        ),
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return {
        "payout_id": str(row.id),
        "status": row.status.value,
        "amount": str(row.amount),
        "note": (
            "Paid through payroll with tax deducted - a referral bonus is "
            "salary income."
        ),
    }


# ══════════════════════════════════════════════════════════════════════════════
# Costs
# ══════════════════════════════════════════════════════════════════════════════


async def record_cost(
    session: AsyncSession,
    user: User,
    *,
    period_label: str,
    component: CostComponentEnum,
    amount: Decimal,
    source: str = "",
    note: str | None = None,
    request: Request | None = None,
) -> RecruitmentCost:
    stmt = tenant_scoped(
        select(RecruitmentCost)
        .where(RecruitmentCost.period_label == period_label)
        .where(RecruitmentCost.component == component)
        .where(RecruitmentCost.source == source),
        RecruitmentCost,
        user,
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        row = RecruitmentCost(
            tenant_id=user.tenant_id,
            period_label=period_label,
            component=component,
            source=source,
            amount=_d(amount),
        )
        session.add(row)
    row.amount = _d(amount)
    row.note = note
    row.recorded_by_user_id = user.id

    await session.commit()
    await session.refresh(row)
    return row


async def cost_report(
    session: AsyncSession, user: User, *, period_label: str, hires: int
) -> dict[str, Any]:
    """Cost per hire from what has been recorded, naming what has not.

    A component with no row is **missing**, not zero. A cost per hire that omits
    agency fees looks excellent and is wrong, and it is the number a client
    repeats to their board.
    """
    rows = list(
        (
            await session.execute(
                tenant_scoped(
                    select(RecruitmentCost).where(
                        RecruitmentCost.period_label == period_label
                    ),
                    RecruitmentCost,
                    user,
                )
            )
        ).scalars()
    )
    components: dict[str, Decimal] = {}
    for row in rows:
        components[row.component.value] = components.get(
            row.component.value, Decimal("0")
        ) + _d(row.amount)

    try:
        return funnel_engine.cost_per_hire(
            hires=hires, components=components, period_label=period_label
        )
    except funnel_engine.FunnelError as exc:
        raise _bad_request(str(exc)) from exc


# ══════════════════════════════════════════════════════════════════════════════
# Diversity
# ══════════════════════════════════════════════════════════════════════════════


async def diversity_report(
    session: AsyncSession,
    user: User,
    *,
    journeys: list[funnel_engine.CandidateJourney],
    dimension: str,
    focus_value: str,
) -> dict[str, Any]:
    """Representation through the funnel, suppressed below the threshold.

    The journeys are supplied by the caller from the application records, so
    this function does not need its own funnel query - and there is deliberately
    no cached copy for it to disagree with.
    """
    rows = list(
        (
            await session.execute(
                tenant_scoped(
                    select(CandidateDiversityDisclosure).where(
                        CandidateDiversityDisclosure.dimension == dimension
                    ),
                    CandidateDiversityDisclosure,
                    user,
                )
            )
        ).scalars()
    )
    return diversity_engine.stage_representation(
        journeys,
        disclosures=[
            diversity_engine.DisclosedAttribute(
                candidate_id=str(r.candidate_id),
                dimension=r.dimension,
                value=r.value,
            )
            for r in rows
        ],
        dimension=dimension,
        focus_value=focus_value,
    )


async def interviewer_signals(
    session: AsyncSession,
    user: User,
    *,
    feedback: list[interviewer_engine.FeedbackFacts],
    interviewer_id: str,
) -> dict[str, Any]:
    """Severity, turnaround and feedback-quality proxies for one interviewer.

    The feedback facts are supplied by the caller from the interview records
    rather than queried here, for the same reason the funnel is derived: there
    is no cached copy to disagree with the source.

    Every section carries the coaching framing. An interviewer who learns their
    pass rate is measured will raise it, and the hires get worse while the
    dashboard improves.
    """
    return {
        "interviewer_id": interviewer_id,
        "severity": interviewer_engine.severity(
            feedback, interviewer_id=interviewer_id
        ),
        "turnaround": interviewer_engine.turnaround(
            feedback, interviewer_id=interviewer_id
        ),
        "feedback_quality": interviewer_engine.feedback_quality(
            feedback, interviewer_id=interviewer_id
        ),
        "framing": interviewer_engine.COACHING_FRAMING,
    }


def recruiting_posture() -> dict[str, Any]:
    """What this slice enforces, and what it does not."""
    return {
        "enforced": [
            "Funnel stages are cumulative, so the counts are monotonic by "
            "construction and a fast-tracked candidate does not break them.",
            "The funnel is derived on every call; there is no summary table to "
            "disagree with the records.",
            "The reconciliation is returned beside the chart, so a discrepancy "
            "is visible rather than rendered.",
            "A cost per hire names the components that were not supplied and "
            "describes itself as a floor.",
            "Interviewer signals carry a coaching framing in the payload, have "
            "no rank or percentile, and report nothing below the sample floor.",
            "The duplicate check runs before a referral is accepted, and the "
            "first-submission rule is in the terms shown before anybody refers.",
            "A referral payout schedule is written in full when the candidate "
            "joins, so a later milestone is visible as owed from day one.",
            "Diversity figures are suppressed below the group threshold and "
            "carry their disclosure coverage.",
        ],
        "not_built": [
            "Nothing in payroll picks up a referral milestone. The payout row "
            "is written with paid_at null and stays visible as owed until "
            "somebody records the payroll reference.",
            "No accounting system is read. Recruitment costs are entered by a "
            "person, and a component nobody entered is missing rather than "
            "zero.",
            "Nothing posts to WhatsApp or LinkedIn. A tracked link is generated "
            "and the employee's own app shares it.",
            "Interviewer signals are not surfaced to the interviewer "
            "themselves, and no coaching conversation is prompted or recorded.",
            "Diversity analytics read whatever disclosures exist; nothing here "
            "collects consent or asks the question.",
            "Retention of referred hires is computed from tenure supplied by "
            "the caller; there is no attrition feed into this module.",
        ],
    }
