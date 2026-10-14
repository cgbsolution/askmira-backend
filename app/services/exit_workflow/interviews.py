"""Exit interviews, the reasons people leave, and whether they could come back.

**Who reads an interview.** HR, and the leaver themselves. Never the leaver's
manager: what somebody says about their manager is only honest when that
manager cannot read it. The summary — reasons and ratings across leavers —
is HR's, and is suppressed below a handful of interviews so that "one person
left Sales in March, rating their manager 1" cannot be read off it.

**Rehire eligibility.** HR's call, and a "no" needs a written reason: an
unexplained flag is a blacklist nobody can examine or challenge. It is shown
to HR when the same person applies again.
"""

from __future__ import annotations

import statistics
import uuid
from collections import Counter
from datetime import UTC, date, datetime
from typing import Any

from fastapi import HTTPException, Request
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.employee import Employee, EmploymentStatus
from app.models.exit_workflow import EXIT_REASONS, ExitInterview, ExitRecord, ExitStatus
from app.models.user import User
from app.services import audit

__all__ = [
    "MIN_FOR_SUMMARY",
    "RATING_KEYS",
    "former_employee",
    "get_interview",
    "interview_summary",
    "record_interview",
    "set_rehire_eligibility",
]

RATING_KEYS = ("manager", "growth", "compensation", "culture", "work_life")
#: Below this many interviews in the window, the summary says so instead of answering.
MIN_FOR_SUMMARY = 3


async def _exit(session: AsyncSession, user: User, exit_id: uuid.UUID) -> ExitRecord:
    record = await session.scalar(
        tenant_scoped(select(ExitRecord).where(ExitRecord.id == exit_id), ExitRecord, user)
    )
    if record is None:
        raise HTTPException(status_code=404, detail="Exit record not found")
    return record


def _read(row: ExitInterview) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "exit_id": str(row.exit_id),
        "mode": row.mode,
        "primary_reason": row.primary_reason,
        "other_reasons": list(row.other_reasons or []),
        "ratings": dict(row.ratings or {}),
        "would_recommend": row.would_recommend,
        "would_return": row.would_return,
        "comments": row.comments,
        "conducted_on": row.conducted_on.isoformat(),
    }


def _validate(values: dict[str, Any]) -> None:
    if values.get("primary_reason") not in EXIT_REASONS:
        raise HTTPException(status_code=422, detail="Choose the main reason from the list.")
    unknown = [r for r in values.get("other_reasons") or [] if r not in EXIT_REASONS]
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unknown reason(s): {', '.join(unknown)}.")
    for key, value in (values.get("ratings") or {}).items():
        if key not in RATING_KEYS or not isinstance(value, int) or not 1 <= value <= 5:
            raise HTTPException(
                status_code=422, detail=f"Ratings are 1 to 5 for {', '.join(RATING_KEYS)}."
            )


async def record_interview(
    session: AsyncSession,
    user: User,
    exit_id: uuid.UUID,
    values: dict[str, Any],
    *,
    request: Request | None = None,
) -> dict[str, Any]:
    """HR records a conversation, or the leaver fills it in themselves. Once per exit."""
    record = await _exit(session, user, exit_id)
    is_leaver = user.employee_id is not None and user.employee_id == record.employee_id
    if not (permissions.is_hr(user) or is_leaver):
        raise permissions.forbidden("The exit interview is for the leaver and HR only.")
    if record.status is ExitStatus.CANCELLED:
        raise HTTPException(status_code=409, detail="This exit was withdrawn.")
    _validate(values)
    row = await session.scalar(
        tenant_scoped(
            select(ExitInterview).where(ExitInterview.exit_id == record.id), ExitInterview, user
        )
    )
    if row is not None and not permissions.is_hr(user):
        raise HTTPException(
            status_code=409, detail="Your exit interview is already in. HR can add to it."
        )
    created = row is None
    if row is None:
        row = ExitInterview(
            tenant_id=user.tenant_id,
            exit_id=record.id,
            employee_id=record.employee_id,
            mode="self" if is_leaver and not permissions.is_hr(user) else "conversation",
            conducted_on=datetime.now(UTC).date(),
            primary_reason=values["primary_reason"],
        )
        session.add(row)
    row.primary_reason = values["primary_reason"]
    row.other_reasons = list(values.get("other_reasons") or [])
    row.ratings = dict(values.get("ratings") or {})
    row.would_recommend = values.get("would_recommend")
    row.would_return = values.get("would_return")
    row.comments = (values.get("comments") or "").strip() or None
    if values.get("conducted_on"):
        row.conducted_on = values["conducted_on"]
    row.recorded_by_user_id = user.id
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.CREATE if created else AuditAction.UPDATE,
        entity_type="exit_interview",
        entity_id=row.id,
        # The answers are not copied into the audit trail: it is read more widely.
        summary=f"Exit interview {'recorded' if created else 'updated'} ({row.mode})",
        request=request,
    )
    return _read(row)


async def get_interview(
    session: AsyncSession, user: User, exit_id: uuid.UUID
) -> dict[str, Any] | None:
    record = await _exit(session, user, exit_id)
    is_leaver = user.employee_id is not None and user.employee_id == record.employee_id
    if not (permissions.is_hr(user) or is_leaver):
        raise permissions.forbidden("The exit interview is for the leaver and HR only.")
    row = await session.scalar(
        tenant_scoped(
            select(ExitInterview).where(ExitInterview.exit_id == record.id), ExitInterview, user
        )
    )
    return _read(row) if row is not None else None


async def interview_summary(
    session: AsyncSession, user: User, *, start: date, end: date
) -> dict[str, Any]:
    """Why people left, across leavers in a window. Withheld below MIN_FOR_SUMMARY."""
    permissions.require_hr(user, detail="The exit summary is HR's.")
    rows = (
        await session.scalars(
            tenant_scoped(
                select(ExitInterview).where(
                    ExitInterview.conducted_on >= start, ExitInterview.conducted_on <= end
                ),
                ExitInterview,
                user,
            )
        )
    ).all()
    base: dict[str, Any] = {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "interviews": len(rows),
        "min_for_summary": MIN_FOR_SUMMARY,
    }
    if len(rows) < MIN_FOR_SUMMARY:
        return base | {
            "withheld": True,
            "note": (
                f"Fewer than {MIN_FOR_SUMMARY} exit interviews in this window. A summary of "
                "so few is a summary of named people."
            ),
        }
    reasons = Counter(r.primary_reason for r in rows)
    ratings = {}
    for key in RATING_KEYS:
        given = [r.ratings[key] for r in rows if isinstance((r.ratings or {}).get(key), int)]
        ratings[key] = (
            {"average": round(statistics.fmean(given), 2), "answers": len(given)}
            if given
            else None
        )

    def share(flag: str) -> str | None:
        answered = [getattr(r, flag) for r in rows if getattr(r, flag) is not None]
        if not answered:
            return None
        return f"{100 * sum(1 for a in answered if a) / len(answered):.0f}"

    return base | {
        "withheld": False,
        "primary_reasons": [
            {"reason": reason, "count": count} for reason, count in reasons.most_common()
        ],
        "ratings": ratings,
        "would_recommend_percent": share("would_recommend"),
        "would_return_percent": share("would_return"),
    }


async def set_rehire_eligibility(
    session: AsyncSession,
    user: User,
    exit_id: uuid.UUID,
    *,
    eligible: bool,
    note: str | None,
    request: Request | None = None,
) -> dict[str, Any]:
    permissions.require_hr(user, detail="Rehire eligibility is HR's to record.")
    record = await _exit(session, user, exit_id)
    clean = (note or "").strip() or None
    if not eligible and clean is None:
        raise HTTPException(
            status_code=422,
            detail="Say why somebody may not be rehired; an unexplained flag cannot be reviewed.",
        )
    before = {"eligible": record.is_rehire_eligible, "note": record.rehire_note}
    record.is_rehire_eligible = eligible
    record.rehire_note = clean
    record.rehire_decided_by_user_id = user.id
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.UPDATE,
        entity_type="exit_record",
        entity_id=record.id,
        summary=f"Rehire eligibility set to {'yes' if eligible else 'no'}",
        changes={"from": before, "to": {"eligible": eligible, "note": clean}},
        request=request,
    )
    return {"exit_id": str(record.id), "is_rehire_eligible": eligible, "rehire_note": clean}


async def former_employee(
    session: AsyncSession, user: User, *, email: str | None, phone: str | None
) -> dict[str, Any] | None:
    """Whether a candidate has worked here before, and how they left. HR only."""
    if not permissions.is_hr(user) or not (email or phone):
        return None
    conditions = []
    if email:
        lowered = email.strip().lower()
        conditions += [
            func.lower(Employee.work_email) == lowered,
            func.lower(Employee.personal_email) == lowered,
        ]
    if phone:
        digits = "".join(ch for ch in phone if ch.isdigit())[-10:]
        if len(digits) == 10:
            conditions.append(
                func.right(func.regexp_replace(Employee.phone, r"\D", "", "g"), 10) == digits
            )
    match = await session.scalar(
        tenant_scoped(
            select(Employee)
            .where(Employee.status == EmploymentStatus.EXITED, or_(*conditions))
            .order_by(Employee.date_of_exit.desc().nulls_last())
            .limit(1),
            Employee,
            user,
        )
    )
    if match is None:
        return None
    record = await session.scalar(
        tenant_scoped(
            select(ExitRecord)
            .where(
                ExitRecord.employee_id == match.id, ExitRecord.status != ExitStatus.CANCELLED
            )
            .order_by(ExitRecord.created_at.desc())
            .limit(1),
            ExitRecord,
            user,
        )
    )
    return {
        "employee_id": str(match.id),
        "employee_code": match.employee_code,
        "date_of_exit": match.date_of_exit.isoformat() if match.date_of_exit else None,
        "exit_type": record.exit_type.value if record else None,
        "is_rehire_eligible": record.is_rehire_eligible if record else None,
        "rehire_note": record.rehire_note if record else None,
    }
