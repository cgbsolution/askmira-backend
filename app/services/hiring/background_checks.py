"""Background verification, from request to outcome, consent first.

1. HR asks for a set of checks and names the agency.
2. The candidate is emailed their portal link and agrees — or declines — by
   typing their name, having been shown exactly what will be checked and by
   whom (DPDP purpose `background_verification`). Nothing is sent before that.
3. The case goes to the agency through its connector. The "manual" connector
   hands nothing over: HR sends the case, and records each check's outcome and
   the agency's report here when it comes back.

An agency connector (AuthBridge, IDfy, SpringVerify, ...) is a `BgvVendor`
registered with `register_vendor`. None is registered beyond "manual": each
needs the tenant's contract and a sandbox to be built against, and an adapter
written without one would be a guess at somebody else's API.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.background_checks import BackgroundCheck, BackgroundCheckStatus
from app.models.hiring import Candidate
from app.models.user import User
from app.services import audit
from app.services.notifications.dispatch import register_event

__all__ = [
    "CHECK_TYPES",
    "RESULT_STATUSES",
    "BgvVendor",
    "cancel_check",
    "list_for_candidate",
    "portal_checks",
    "record_results",
    "register_vendor",
    "report_file",
    "request_check",
    "respond_to_check",
    "upload_report",
    "vendors",
]

CHECK_TYPES: dict[str, str] = {
    "identity": "Identity (PAN, Aadhaar)",
    "address": "Current address",
    "education": "Highest qualification",
    "employment": "Previous employment",
    "criminal": "Court records",
    "reference": "Professional references",
}
RESULT_STATUSES = ("clear", "discrepancy", "unable_to_verify")
OPEN = (BackgroundCheckStatus.AWAITING_CONSENT, BackgroundCheckStatus.IN_PROGRESS)

BGV_CONSENT = "hiring.candidate.bgv_consent"
register_event(
    BGV_CONSENT,
    "{organisation} would like your consent for background checks",
    "Hello {first_name},\n\nAs part of your application for {job_title}, {organisation} would "
    "like to verify: {checks}. The checks are done by {agency}.\n\nNothing starts until you "
    "agree. Read exactly what will be checked, and agree or decline, here:\n{link}",
)


class BgvVendor(Protocol):
    key: str
    label: str

    async def initiate(self, check: BackgroundCheck, candidate: Candidate) -> str | None:
        """Hand the case to the agency; return its reference, or None if HR sends it."""
        ...


@dataclass(frozen=True)
class _Manual:
    key: str = "manual"
    label: str = "Any agency — HR sends the case and records the outcome"

    async def initiate(self, check: BackgroundCheck, candidate: Candidate) -> str | None:
        return None


_VENDORS: dict[str, BgvVendor] = {"manual": _Manual()}


def register_vendor(vendor: BgvVendor) -> None:
    _VENDORS[vendor.key] = vendor


def vendors() -> list[dict[str, str]]:
    return [{"key": v.key, "label": v.label} for v in _VENDORS.values()]


def _read(row: BackgroundCheck) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "candidate_id": str(row.candidate_id),
        "offer_id": str(row.offer_id) if row.offer_id else None,
        "vendor": row.vendor,
        "agency_name": row.agency_name,
        "vendor_reference": row.vendor_reference,
        "status": row.status.value,
        "checks": [
            {**c, "label": CHECK_TYPES.get(c.get("type", ""), c.get("type", ""))}
            for c in row.checks or []
        ],
        "consent_notice": row.consent_notice,
        "consent_given_at": row.consent_given_at.isoformat() if row.consent_given_at else None,
        "consent_typed_name": row.consent_typed_name,
        "summary": row.summary,
        "has_report": row.report_storage_key is not None,
        "report_filename": row.report_filename,
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        "cancelled_reason": row.cancelled_reason,
        "created_at": row.created_at.isoformat(),
    }


def _require_hr(user: User) -> None:
    permissions.require_hr(user, detail="Background checks are handled by HR.")


async def _candidate(session: AsyncSession, user: User, candidate_id: uuid.UUID) -> Candidate:
    candidate = await session.scalar(
        tenant_scoped(select(Candidate).where(Candidate.id == candidate_id), Candidate, user)
    )
    if candidate is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate not found")
    return candidate


async def _check(session: AsyncSession, user: User, check_id: uuid.UUID) -> BackgroundCheck:
    row = await session.scalar(
        tenant_scoped(
            select(BackgroundCheck).where(BackgroundCheck.id == check_id), BackgroundCheck, user
        )
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Background check not found")
    return row


def _notice(check_types: list[str], agency: str, organisation: str) -> str:
    listed = "; ".join(CHECK_TYPES[t] for t in check_types)
    return (
        f"{organisation} will verify the following, through {agency}: {listed}. The agency "
        "will contact the institutions and employers concerned, and may ask you for "
        "documents. The outcome is seen by HR only and is kept with your application. "
        "You can decline; declining may mean the offer cannot proceed."
    )


async def list_for_candidate(
    session: AsyncSession, user: User, candidate_id: uuid.UUID
) -> list[dict[str, Any]]:
    _require_hr(user)
    await _candidate(session, user, candidate_id)
    rows = (
        await session.scalars(
            tenant_scoped(
                select(BackgroundCheck)
                .where(BackgroundCheck.candidate_id == candidate_id)
                .order_by(BackgroundCheck.created_at.desc()),
                BackgroundCheck,
                user,
            )
        )
    ).all()
    return [_read(r) for r in rows]


async def request_check(
    session: AsyncSession,
    user: User,
    *,
    candidate_id: uuid.UUID,
    check_types: list[str],
    agency_name: str,
    vendor: str = "manual",
    offer_id: uuid.UUID | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    """Ask the candidate for consent. Nothing goes to the agency until they give it."""
    from app.models.tenant import Tenant
    from app.services.hiring import candidate_comms, careers

    _require_hr(user)
    candidate = await _candidate(session, user, candidate_id)
    chosen = list(dict.fromkeys(check_types))
    unknown = [t for t in chosen if t not in CHECK_TYPES]
    if not chosen or unknown:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Unknown check type(s): {', '.join(unknown)}." if unknown
                else "Choose at least one check."
            ),
        )
    if vendor not in _VENDORS:
        raise HTTPException(status_code=422, detail=f"No connector called '{vendor}'.")
    if not agency_name.strip():
        raise HTTPException(
            status_code=422,
            detail="Name the agency: the candidate is told who will check before they agree.",
        )
    open_check = await session.scalar(
        tenant_scoped(
            select(BackgroundCheck.id).where(
                BackgroundCheck.candidate_id == candidate.id, BackgroundCheck.status.in_(OPEN)
            ),
            BackgroundCheck,
            user,
        )
    )
    if open_check is not None:
        raise HTTPException(
            status_code=409,
            detail="This candidate already has a background check open. Finish or cancel it.",
        )

    tenant = await session.get(Tenant, user.tenant_id)
    row = BackgroundCheck(
        tenant_id=user.tenant_id,
        candidate_id=candidate.id,
        offer_id=offer_id,
        vendor=vendor,
        agency_name=agency_name.strip(),
        checks=[{"type": t, "status": "pending", "note": None} for t in chosen],
        status=BackgroundCheckStatus.AWAITING_CONSENT,
        consent_notice=_notice(chosen, agency_name.strip(), tenant.name if tenant else "We"),
        requested_by_user_id=user.id,
    )
    session.add(row)
    await session.flush()

    _portal, token = await careers.issue_portal_token(session, user, candidate_id=candidate.id)
    await candidate_comms.on_portal_link(
        session, user, candidate=candidate, token=token, event_key=BGV_CONSENT,
        payload={
            "checks": ", ".join(CHECK_TYPES[t].lower() for t in chosen),
            "agency": row.agency_name,
        },
    )
    await audit.record(
        session, tenant_id=user.tenant_id, actor=user, action=AuditAction.CREATE,
        entity_type="background_check", entity_id=row.id,
        summary=(
            f"Background check requested through {row.agency_name} "
            f"({', '.join(chosen)}); awaiting the candidate's consent"
        ),
        request=request,
    )
    return _read(row)


async def respond_to_check(
    session: AsyncSession,
    token: str,
    check_id: uuid.UUID,
    *,
    agree: bool,
    typed_name: str | None,
    ip_address: str | None,
    user_agent: str | None,
) -> dict[str, Any]:
    """The candidate's answer, from their portal: agree by typing their name, or decline."""
    from app.services.hiring import candidate_portal, careers

    portal = await careers.resolve_portal_token(session, token)
    actor = candidate_portal._portal_actor(portal.tenant_id)
    row = await session.scalar(
        tenant_scoped(
            select(BackgroundCheck).where(
                BackgroundCheck.id == check_id,
                BackgroundCheck.candidate_id == portal.candidate_id,
            ),
            BackgroundCheck,
            actor,
        )
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Background check not found")
    if row.status is not BackgroundCheckStatus.AWAITING_CONSENT:
        raise HTTPException(
            status_code=409, detail="You have already answered this request."
        )
    candidate = await _candidate(session, actor, portal.candidate_id)

    if agree:
        typed = " ".join((typed_name or "").split())
        if typed.lower() != " ".join(candidate.full_name.split()).lower():
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Type your full name as it appears on your application "
                    f"({candidate.full_name}) to agree."
                ),
            )
        row.consent_given_at = datetime.now(UTC)
        row.consent_typed_name = typed
        row.consent_ip = ip_address
        row.consent_user_agent = (user_agent or "")[:512] or None
        row.status = BackgroundCheckStatus.IN_PROGRESS
        row.vendor_reference = await _VENDORS[row.vendor].initiate(row, candidate)
        verb = "agreed to"
    else:
        row.status = BackgroundCheckStatus.CANCELLED
        row.cancelled_reason = "The candidate declined consent."
        verb = "declined"
    await session.flush()
    await audit.record(
        session, tenant_id=portal.tenant_id, actor_type="candidate", action=AuditAction.UPDATE,
        entity_type="background_check", entity_id=row.id,
        changes={"response": "agreed" if agree else "declined", "via": "candidate_portal"},
        summary=f"Candidate {verb} the background check in the portal",
    )
    return _portal_read(row)


def _portal_read(row: BackgroundCheck) -> dict[str, Any]:
    """What the candidate sees: the request and their answer, never the outcome."""
    return {
        "id": str(row.id),
        "agency_name": row.agency_name,
        "checks": [CHECK_TYPES.get(c.get("type", ""), "") for c in row.checks or []],
        "notice": row.consent_notice,
        "status": (
            "awaiting_your_answer"
            if row.status is BackgroundCheckStatus.AWAITING_CONSENT
            else "declined"
            if row.status is BackgroundCheckStatus.CANCELLED and row.consent_given_at is None
            else "agreed"
        ),
        "answered_at": row.consent_given_at.isoformat() if row.consent_given_at else None,
    }


async def portal_checks(session: AsyncSession, token: str) -> list[dict[str, Any]]:
    from app.services.hiring import candidate_portal, careers

    portal = await careers.resolve_portal_token(session, token)
    actor = candidate_portal._portal_actor(portal.tenant_id)
    rows = (
        await session.scalars(
            tenant_scoped(
                select(BackgroundCheck)
                .where(BackgroundCheck.candidate_id == portal.candidate_id)
                .order_by(BackgroundCheck.created_at.desc()),
                BackgroundCheck,
                actor,
            )
        )
    ).all()
    return [_portal_read(r) for r in rows]


async def record_results(
    session: AsyncSession,
    user: User,
    check_id: uuid.UUID,
    *,
    results: list[dict[str, Any]],
    summary: str | None,
    request: Request | None = None,
) -> dict[str, Any]:
    """Record what the agency found, check by check. Settles once every check is in."""
    _require_hr(user)
    row = await _check(session, user, check_id)
    if row.status is not BackgroundCheckStatus.IN_PROGRESS:
        raise HTTPException(
            status_code=409,
            detail=(
                "Outcomes are recorded once the candidate has agreed and before the check "
                f"is settled; this one is {row.status.value.replace('_', ' ')}."
            ),
        )
    by_type = {c["type"]: dict(c) for c in row.checks}
    for result in results:
        kind, outcome = result.get("type"), result.get("status")
        if kind not in by_type:
            raise HTTPException(status_code=422, detail=f"'{kind}' is not part of this check.")
        if outcome not in (*RESULT_STATUSES, "pending"):
            raise HTTPException(status_code=422, detail=f"'{outcome}' is not an outcome.")
        if outcome == "discrepancy" and not (result.get("note") or "").strip():
            raise HTTPException(
                status_code=422,
                detail="A discrepancy needs a note saying what differed.",
            )
        by_type[kind]["status"] = outcome
        by_type[kind]["note"] = (result.get("note") or "").strip() or None
    row.checks = [by_type[c["type"]] for c in row.checks]
    if summary is not None:
        row.summary = summary.strip() or None

    outcomes = {c["status"] for c in row.checks}
    if "pending" not in outcomes:
        row.status = (
            BackgroundCheckStatus.DISCREPANCY
            if "discrepancy" in outcomes
            else BackgroundCheckStatus.UNABLE_TO_VERIFY
            if "unable_to_verify" in outcomes
            else BackgroundCheckStatus.CLEAR
        )
        row.completed_at = datetime.now(UTC)
        row.completed_by_user_id = user.id
    await session.flush()
    await audit.record(
        session, tenant_id=user.tenant_id, actor=user, action=AuditAction.UPDATE,
        entity_type="background_check", entity_id=row.id,
        summary=f"Background check outcomes recorded; status {row.status.value}",
        changes={"checks": row.checks}, request=request,
    )
    return _read(row)


async def upload_report(
    session: AsyncSession,
    user: User,
    check_id: uuid.UUID,
    *,
    data: bytes,
    filename: str,
    request: Request | None = None,
) -> dict[str, Any]:
    from app.services.hiring import resume_files

    _require_hr(user)
    row = await _check(session, user, check_id)
    if row.status in (BackgroundCheckStatus.AWAITING_CONSENT, BackgroundCheckStatus.CANCELLED):
        raise HTTPException(
            status_code=409, detail="A report belongs to a check the candidate agreed to."
        )
    key, _kind = await resume_files.accept_document(data, user.tenant_id)
    row.report_storage_key = key
    row.report_filename = filename[:255] or "report"
    await session.flush()
    await audit.record(
        session, tenant_id=user.tenant_id, actor=user, action=AuditAction.UPDATE,
        entity_type="background_check", entity_id=row.id,
        summary="Background check report attached", request=request,
    )
    return _read(row)


async def report_file(
    session: AsyncSession, user: User, check_id: uuid.UUID
) -> tuple[bytes, str]:
    from app.services.storage import get_storage

    _require_hr(user)
    row = await _check(session, user, check_id)
    if row.report_storage_key is None:
        raise HTTPException(status_code=404, detail="No report has been attached.")
    data = await get_storage().get(row.report_storage_key)
    if data is None:
        raise HTTPException(status_code=404, detail="The report file is missing from storage.")
    return data, row.report_filename or "report"


async def cancel_check(
    session: AsyncSession, user: User, check_id: uuid.UUID, *, reason: str,
    request: Request | None = None,
) -> dict[str, Any]:
    _require_hr(user)
    row = await _check(session, user, check_id)
    if row.status not in OPEN:
        raise HTTPException(status_code=409, detail="Only an open check can be cancelled.")
    if not reason.strip():
        raise HTTPException(status_code=422, detail="Say why the check is being cancelled.")
    row.status = BackgroundCheckStatus.CANCELLED
    row.cancelled_reason = reason.strip()
    await session.flush()
    await audit.record(
        session, tenant_id=user.tenant_id, actor=user, action=AuditAction.UPDATE,
        entity_type="background_check", entity_id=row.id,
        summary=f"Background check cancelled: {reason.strip()}", request=request,
    )
    return _read(row)
