"""The referral screens: open roles, programmes, referring, and who is owed what.

Referring is in your own name, always: the referrer is the caller's own employee
record, taken from the token. Whether they are on a PIP or on the hiring team for
the role is worked out here from the records — never taken from the request, where
the person asking would be the one declaring themselves eligible.

A referral that names a candidate lands them in the pipeline through the same
ingestion and duplicate check as any other application, with `referral` as the
source. No consent is recorded for them: the referrer gave the details, and the
candidate agreed to nothing yet. Their profile says so.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.employee import Employee
from app.models.hiring import Requisition, RequisitionStatus
from app.models.jobboard import JobBoard
from app.models.org import Department, Location
from app.models.recruiting import (
    RecruitmentCost,
    Referral,
    ReferralPayout,
    ReferralProgramme,
    ShareChannelEnum,
)
from app.models.user import User
from app.services.hiring import ingestion
from app.services.jobboards.base import RawApplication
from app.services.recruiting import service as recruiting

__all__ = [
    "all_programmes",
    "all_referrals",
    "list_costs",
    "my_referrals",
    "open_roles",
    "programme_terms_for",
    "refer_candidate",
]

_DUPLICATE_RULE = "If two people refer the same candidate, the first submission wins by timestamp."


def _name(employee: Employee | None) -> str | None:
    if employee is None:
        return None
    return employee.display_name or " ".join(
        p for p in (employee.first_name, employee.last_name) if p
    )


async def open_roles(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    """Approved, open roles anybody in the company may refer into. No budget."""
    rows = (
        await session.execute(
            tenant_scoped(
                select(Requisition, Department.name, Location.name)
                .outerjoin(Department, Department.id == Requisition.department_id)
                .outerjoin(Location, Location.id == Requisition.location_id)
                .where(Requisition.status == RequisitionStatus.OPEN)
                .order_by(Requisition.created_at.desc()),
                Requisition,
                user,
            )
        )
    ).all()
    return [
        {
            "id": str(r.id),
            "code": r.code,
            "title": r.title,
            "department": department,
            "location": location,
            "employment_type": r.employment_type,
            "experience_min_years": r.experience_min_years,
            "experience_max_years": r.experience_max_years,
            "open_vacancies": r.open_vacancies,
            "posted_on": r.created_at.date().isoformat(),
        }
        for r, department, location in rows
    ]


def programme_terms_for(programme: ReferralProgramme) -> dict[str, Any]:
    return {
        "id": str(programme.id),
        "name": programme.name,
        "description": programme.description,
        "is_active": programme.is_active,
        "eligible_roles": list(programme.eligible_roles or []),
        "bonus_by_grade": dict(programme.bonus_by_grade or {}),
        "milestones": list(programme.payout_milestones or []),
        "rules": {
            "duplicate_rule": _DUPLICATE_RULE,
            "require_confirmed": programme.require_confirmed,
            "exclude_on_pip": programme.exclude_on_pip,
            "exclude_hiring_team": programme.exclude_hiring_team,
        },
        "tax_note": (
            "A referral bonus is salary income and is paid through payroll with tax "
            "deducted. The amounts above are gross."
        ),
        "payroll_note": (
            "Payouts are recorded here when HR marks a milestone paid with its payroll "
            "reference. Payroll does not pick them up automatically yet."
        ),
    }


async def all_programmes(
    session: AsyncSession, user: User, *, active_only: bool
) -> list[dict[str, Any]]:
    stmt = select(ReferralProgramme).order_by(ReferralProgramme.name)
    if active_only:
        stmt = stmt.where(ReferralProgramme.is_active.is_(True))
    rows = await session.scalars(tenant_scoped(stmt, ReferralProgramme, user))
    return [programme_terms_for(p) for p in rows.all()]


async def _payouts_by_referral(
    session: AsyncSession, user: User, referral_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[dict[str, Any]]]:
    if not referral_ids:
        return {}
    rows = await session.scalars(
        tenant_scoped(
            select(ReferralPayout)
            .where(ReferralPayout.referral_id.in_(referral_ids))
            .order_by(ReferralPayout.payable_on),
            ReferralPayout,
            user,
        )
    )
    grouped: dict[uuid.UUID, list[dict[str, Any]]] = {}
    for p in rows.all():
        grouped.setdefault(p.referral_id, []).append(
            {
                "id": str(p.id),
                "milestone": p.milestone_label,
                "amount": str(p.amount),
                "payable_on": p.payable_on.isoformat(),
                "status": p.status.value,
                "paid_at": p.paid_at.isoformat() if p.paid_at else None,
                "payroll_reference": p.payroll_reference,
            }
        )
    return grouped


async def my_referrals(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    """The caller's own referrals and what each has earned so far."""
    me = user.employee_id
    if me is None:
        return []
    rows = (
        await session.execute(
            tenant_scoped(
                select(Referral, Requisition.code, Requisition.title)
                .outerjoin(Requisition, Requisition.id == Referral.requisition_id)
                .where(Referral.referrer_employee_id == me)
                .order_by(Referral.submitted_at.desc()),
                Referral,
                user,
            )
        )
    ).all()
    payouts = await _payouts_by_referral(session, user, [r.id for r, _, _ in rows])
    winners = {r.duplicate_of_id for r, _, _ in rows if r.duplicate_of_id is not None}
    winner_dates: dict[uuid.UUID, str] = {}
    if winners:
        winner_dates = {
            w.id: w.submitted_at.isoformat()
            for w in (
                await session.scalars(
                    tenant_scoped(select(Referral).where(Referral.id.in_(winners)), Referral, user)
                )
            ).all()
        }
    return [
        {
            "id": str(r.id),
            "candidate": r.candidate_key,
            "requisition_code": code,
            "requisition_title": title,
            "status": r.status.value,
            "submitted_at": r.submitted_at.isoformat(),
            "duplicate_note": (
                "Somebody referred this person first, on "
                f"{winner_dates.get(r.duplicate_of_id, 'an earlier date')}. "
                f"{_DUPLICATE_RULE}"
                if r.duplicate_of_id
                else None
            ),
            "ineligibility_reason": r.ineligibility_reason,
            "bonus_amount": str(r.bonus_amount) if r.bonus_amount else None,
            "hired_on": r.hired_on.isoformat() if r.hired_on else None,
            "payouts": payouts.get(r.id, []),
        }
        for r, code, title in rows
    ]


async def refer_candidate(
    session: AsyncSession,
    user: User,
    *,
    programme_id: uuid.UUID,
    requisition_id: uuid.UUID,
    candidate_name: str,
    candidate_email: str,
    candidate_phone: str | None,
    channel: ShareChannelEnum | None,
    note: str | None,
    request: Request | None = None,
) -> dict[str, Any]:
    """Refer somebody into an open role, in your own name."""
    me = permissions.own_employee_id(user)
    requisition = await session.scalar(
        tenant_scoped(
            select(Requisition).where(Requisition.id == requisition_id), Requisition, user
        )
    )
    if requisition is None or requisition.status is not RequisitionStatus.OPEN:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="That role is not open for referrals.",
        )

    # The candidate enters the pipeline first, unflushed to the outside world:
    # `submit_referral` commits both together, and if the referrer turns out to be
    # ineligible it raises before the commit and neither is kept.
    outcome = await ingestion.ingest_application(
        session,
        user,
        RawApplication(
            external_application_id="",
            board=JobBoard.REFERRAL.value,
            external_job_id="",
            full_name=candidate_name.strip(),
            email=candidate_email,
            phone=candidate_phone,
            applied_at=datetime.now(UTC),
            raw={"referred_by_employee_id": str(me), "note": (note or "")[:1000]},
        ),
        requisition_id=requisition.id,
        consent_purposes=[],
    )
    if outcome.candidate is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=outcome.note)

    result = await recruiting.submit_referral(
        session,
        user,
        programme_id=programme_id,
        referrer_employee_id=me,
        candidate_key=candidate_email,
        requisition_id=requisition.id,
        candidate_id=outcome.candidate.id,
        role_key=requisition.code,
        channel=channel,
        request=request,
    )
    return result | {
        "candidate_added": outcome.created_candidate,
        "role": {"code": requisition.code, "title": requisition.title},
    }


async def all_referrals(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    """Every referral, for HR, with referrer names and the payout schedule."""
    rows = (
        await session.execute(
            tenant_scoped(
                select(Referral, Employee, Requisition.code)
                .join(Employee, Employee.id == Referral.referrer_employee_id)
                .outerjoin(Requisition, Requisition.id == Referral.requisition_id)
                .order_by(Referral.submitted_at.desc()),
                Referral,
                user,
            )
        )
    ).all()
    payouts = await _payouts_by_referral(session, user, [r.id for r, _, _ in rows])
    return [
        {
            "id": str(r.id),
            "programme_id": str(r.programme_id),
            "referrer": _name(referrer),
            "referrer_grade": referrer.grade,
            "candidate": r.candidate_key,
            "candidate_id": str(r.candidate_id) if r.candidate_id else None,
            "requisition_code": code,
            "status": r.status.value,
            "submitted_at": r.submitted_at.isoformat(),
            "duplicate_of_id": str(r.duplicate_of_id) if r.duplicate_of_id else None,
            "bonus_amount": str(r.bonus_amount) if r.bonus_amount else None,
            "hired_on": r.hired_on.isoformat() if r.hired_on else None,
            "payouts": payouts.get(r.id, []),
        }
        for r, referrer, code in rows
    ]


async def list_costs(
    session: AsyncSession, user: User, *, period_label: str | None
) -> list[dict[str, Any]]:
    stmt = select(RecruitmentCost).order_by(
        RecruitmentCost.period_label.desc(), RecruitmentCost.component
    )
    if period_label:
        stmt = stmt.where(RecruitmentCost.period_label == period_label)
    rows = await session.scalars(tenant_scoped(stmt, RecruitmentCost, user))
    return [
        {
            "id": str(c.id),
            "period_label": c.period_label,
            "component": c.component.value,
            "source": c.source,
            "amount": str(c.amount),
            "note": c.note,
        }
        for c in rows.all()
    ]
