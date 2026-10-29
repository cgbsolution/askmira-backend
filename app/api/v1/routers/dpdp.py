"""DPDP compliance endpoints (slice 25).

Three audiences, and the split matters:

- **`/dpdp/me/...`** — the data principal's own view. Any authenticated user
  reaches their own consent state, their own notice, their own export. No role
  gate, because a right you need HR's permission to exercise is not a right.
- **`/dpdp/...`** — the tenant's compliance surface. HR and admin.
- **`/dpdp/breaches/...`** — incident handling. Admin only, and a rehearsal is
  flagged so it can never be counted as a real incident.

The router is thin, as everywhere: parse, authorise, delegate, serialise.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.deps import CurrentUser, DbSession, require_roles, tenant_scoped
from app.models.dpdp import (
    ConsentCapturedVia,
    DataPrincipalKind,
    DataPrincipalRequest,
    DprRequestStatus,
    DprRequestType,
)
from app.models.user import UserRole
from app.services.dpdp import erasure as erasure_service
from app.services.dpdp import service as dpdp_service
from app.services.dpdp import transfers as transfer_engine
from app.services.dpdp.breach import rehearse
from app.services.dpdp.categories import CATEGORIES
from app.services.dpdp.consent import PURPOSES
from app.services.dpdp.inventory import (
    build_inventory,
    coverage_gaps,
    record_of_processing,
)

router = APIRouter(tags=["dpdp"])

HR_ROLES = (UserRole.ADMIN, UserRole.SUB_ADMIN)
ADMIN_ROLES = (UserRole.ADMIN,)


# ── Contracts ───────────────────────────────────────────────────────────────


class ConfigurationIn(BaseModel):
    grievance_contact: str = Field(min_length=1, max_length=200)
    data_protection_officer_name: str | None = Field(default=None, max_length=200)
    #: Per request type, in days. Validated against the statutory outer bound
    #: before it is written — a settings screen cannot commit a tenant to an
    #: unlawful response period.
    sla_days: dict[str, int] = Field(default_factory=dict)
    default_notice_language: str = "en"
    ai_processing_enabled: bool = False


class ConsentIn(BaseModel):
    purpose_key: str = Field(min_length=1, max_length=64)
    granted: bool
    captured_via: ConsentCapturedVia = ConsentCapturedVia.SELF_SERVICE
    notice_id: str | None = None
    notice_language: str = "en"


class ConsentForPrincipalIn(ConsentIn):
    principal_id: uuid.UUID
    principal_kind: DataPrincipalKind = DataPrincipalKind.EMPLOYEE


class RequestIn(BaseModel):
    request_type: DprRequestType
    details: str | None = Field(default=None, max_length=4000)


class RequestForPrincipalIn(RequestIn):
    principal_id: uuid.UUID
    principal_kind: DataPrincipalKind = DataPrincipalKind.EMPLOYEE
    principal_label: str = Field(min_length=1, max_length=200)
    principal_contact: str | None = Field(default=None, max_length=200)


class RequestResolveIn(BaseModel):
    status: DprRequestStatus
    response_payload: dict[str, Any] | None = None
    rejection_reason: str | None = Field(default=None, max_length=4000)
    note: str | None = Field(default=None, max_length=2000)


class BreachIn(BaseModel):
    reference: str = Field(min_length=1, max_length=40)
    became_aware_at: datetime
    detected_at: datetime | None = None
    description: str = Field(min_length=1)
    affected_data_categories: list[str] = Field(default_factory=list)
    affected_principal_count: int = Field(ge=0)
    #: Whether the data reached somebody outside our control. Separate from the
    #: count: an internal over-permission and a public dump are different
    #: incidents at the same headcount, and the grading treats them differently.
    data_left_our_control: bool
    is_rehearsal: bool = False


class TransferIn(BaseModel):
    sub_processor_key: str = Field(min_length=1, max_length=64)
    purpose_key: str = Field(min_length=1, max_length=64)
    category_keys: list[str] = Field(default_factory=list)
    principal_count: int = Field(ge=1)
    description: str = Field(min_length=1)


# ── The catalogue: what we hold and why ─────────────────────────────────────


@router.get("/dpdp/categories")
async def list_categories(user: CurrentUser) -> dict[str, Any]:
    """Every category of personal data, with its lawful basis and retention."""
    return {"categories": [c.as_dict() for c in CATEGORIES.values()]}


@router.get("/dpdp/purposes")
async def list_purposes(user: CurrentUser) -> dict[str, Any]:
    """Every processing purpose, split by whether there is a choice.

    The split is the point. A screen listing all sixteen with a checkbox next to
    each would be offering choices that do not exist.
    """
    everything = [p.as_dict() for p in PURPOSES.values()]
    return {
        "with_a_choice": [p for p in everything if p["requires_consent"]],
        "no_choice": [p for p in everything if not p["requires_consent"]],
    }


@router.get("/dpdp/inventory")
async def data_inventory(
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    """The data inventory, generated from the classification."""
    return build_inventory(dpdp_service.current_schema_shape()).as_dict()


@router.get("/dpdp/processing-record")
async def processing_record(
    _user=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    """The Record of Processing Activities."""
    schema = dpdp_service.current_schema_shape()
    return {
        "activities": [a.as_dict() for a in record_of_processing(schema)],
        "classification_gaps": [str(g) for g in coverage_gaps(schema)],
    }


# ── Configuration ───────────────────────────────────────────────────────────


@router.get("/dpdp/configuration")
async def get_configuration(
    session: DbSession,
    user: CurrentUser,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> dict[str, Any]:
    config = await dpdp_service.get_configuration(session, user)
    if config is None:
        return {"configured": False}
    return {
        "configured": True,
        "grievance_contact": config.grievance_contact,
        "data_protection_officer_name": config.data_protection_officer_name,
        "sla_days": config.sla_days,
        "default_notice_language": config.default_notice_language,
        "ai_processing_enabled": config.ai_processing_enabled,
    }


@router.put("/dpdp/configuration")
async def set_configuration(
    payload: ConfigurationIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> dict[str, Any]:
    config = await dpdp_service.upsert_configuration(
        session,
        user,
        grievance_contact=payload.grievance_contact,
        data_protection_officer_name=payload.data_protection_officer_name,
        sla_days=payload.sla_days,
        default_notice_language=payload.default_notice_language,
        ai_processing_enabled=payload.ai_processing_enabled,
        request=request,
    )
    return {"id": str(config.id), "grievance_contact": config.grievance_contact}


# ── The principal's own view ────────────────────────────────────────────────


@router.get("/dpdp/status")
async def privacy_setup_status(session: DbSession, user: CurrentUser) -> dict[str, Any]:
    """Whether the workspace has named its grievance contact. Drives the admin banner."""
    return await dpdp_service.configuration_status(session, user)


@router.get("/dpdp/me/notice")
async def my_notice(
    session: DbSession, user: CurrentUser, language: str | None = None
) -> dict[str, Any]:
    """The consent notice, itemised, in the language asked for.

    A language we have not translated returns English with `is_fallback` true
    and a line in the notice body saying so — never a silent substitution.
    """
    return await dpdp_service.notice_for(session, user, language=language)


@router.get("/dpdp/me/consent")
async def my_consent(session: DbSession, user: CurrentUser) -> dict[str, Any]:
    """Where every purpose stands for the caller.

    No role gate. A right you need HR's permission to exercise is not a right.
    """
    principal = user.employee_id or user.id
    return await dpdp_service.list_consent(session, user, principal_id=principal)


@router.post("/dpdp/me/consent", status_code=status.HTTP_201_CREATED)
async def record_my_consent(
    payload: ConsentIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
) -> dict[str, Any]:
    """Record the caller's own decision.

    A purpose whose basis is not consent is refused with a 400 naming the actual
    basis. The error is aimed at whoever built the screen that offered the
    checkbox, because that is where the bug is.
    """
    principal = user.employee_id or user.id
    row = await dpdp_service.record_consent(
        session,
        user,
        principal_id=principal,
        principal_kind=(
            DataPrincipalKind.EMPLOYEE if user.employee_id else DataPrincipalKind.PLATFORM_USER
        ),
        purpose_key=payload.purpose_key,
        granted=payload.granted,
        captured_via=payload.captured_via,
        notice_id=payload.notice_id,
        notice_language=payload.notice_language,
        request=request,
    )
    return {"id": str(row.id), "purpose": row.purpose_key, "granted": row.granted}


@router.post("/dpdp/me/consent/{purpose_key}/withdraw")
async def withdraw_my_consent(
    purpose_key: str,
    session: DbSession,
    user: CurrentUser,
    request: Request,
) -> dict[str, Any]:
    """Withdraw, and be told what stopped, what was deleted and what was kept.

    DPDP s.8(7)(a): what was held only under this consent is erased with the
    withdrawal, in the same transaction; what another basis still covers is
    named in `retained` with the reason.
    """
    principal = user.employee_id or user.id
    return await dpdp_service.withdraw_consent(
        session,
        user,
        principal_id=principal,
        principal_kind=(
            DataPrincipalKind.EMPLOYEE if user.employee_id else DataPrincipalKind.PLATFORM_USER
        ),
        purpose_key=purpose_key,
        request=request,
    )


@router.get("/dpdp/me/export")
async def my_export(session: DbSession, user: CurrentUser) -> dict[str, Any]:
    """Everything held about the caller, organised for a person to read."""
    principal = user.employee_id or user.id
    return await dpdp_service.build_access_export(session, user, principal_id=principal)


@router.get("/dpdp/me/export/download")
async def download_my_export(
    session: DbSession, user: CurrentUser, request: Request
) -> JSONResponse:
    """Everything held about the caller, as a file. Audited before it is sent."""
    principal = user.employee_id or user.id
    document, filename = await dpdp_service.export_file(
        session, user, principal_id=principal, request=request
    )
    return JSONResponse(
        document, headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


@router.get("/dpdp/me/requests")
async def my_requests(session: DbSession, user: CurrentUser) -> dict[str, Any]:
    """The caller's own rights requests and what came of them."""
    _ = user.id  # the caller's own requests only
    return {"requests": await dpdp_service.list_my_requests(session, user)}


@router.get("/dpdp/me/erasure-preview")
async def my_erasure_preview(session: DbSession, user: CurrentUser) -> dict[str, Any]:
    """What an erasure request would and would not delete, before asking.

    Deliberately available before the request is made. Somebody deciding whether
    to ask should be able to see that their payroll records will survive, and
    why, rather than discovering it in the response six weeks later.
    """
    principal = user.employee_id or user.id
    return await dpdp_service.preview_erasure(session, user, principal_id=principal)


@router.post("/dpdp/me/requests", status_code=status.HTTP_201_CREATED)
async def raise_my_request(
    payload: RequestIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
) -> dict[str, Any]:
    """Exercise a right. The clock starts now, not after identity checks."""
    principal = user.employee_id or user.id
    row = await dpdp_service.raise_request(
        session,
        user,
        principal_id=principal,
        principal_kind=(
            DataPrincipalKind.EMPLOYEE if user.employee_id else DataPrincipalKind.PLATFORM_USER
        ),
        principal_label=user.full_name or user.email,
        principal_contact=user.email,
        request_type=payload.request_type,
        details=payload.details,
        request=request,
    )
    return {
        "id": str(row.id),
        "type": row.request_type.value,
        "due_by": row.due_by.isoformat(),
        "sla_days": row.sla_days,
    }


# ── The tenant's queue ──────────────────────────────────────────────────────


@router.post("/dpdp/consent", status_code=status.HTTP_201_CREATED)
async def record_consent_for(
    payload: ConsentForPrincipalIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    """Record a decision on somebody's behalf — a signed paper form, say.

    `captured_via` is a distinct value for this so a report can single it out.
    Recording consent on behalf of somebody is legitimate for a form they
    actually signed and is not legitimate as a bulk default, and the two are
    indistinguishable unless the capture route is stored.
    """
    row = await dpdp_service.record_consent(
        session,
        user,
        principal_id=payload.principal_id,
        principal_kind=payload.principal_kind,
        purpose_key=payload.purpose_key,
        granted=payload.granted,
        captured_via=payload.captured_via,
        notice_id=payload.notice_id,
        notice_language=payload.notice_language,
        request=request,
    )
    return {"id": str(row.id), "purpose": row.purpose_key, "granted": row.granted}


@router.get("/dpdp/consent/summary")
async def consent_summary(
    session: DbSession,
    user: CurrentUser,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    """Consent across the workforce: granted, declined, never asked."""
    return await dpdp_service.consent_summary(session, user)


@router.get("/dpdp/consent/{principal_id}")
async def consent_for_principal(
    principal_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    return await dpdp_service.list_consent(session, user, principal_id=principal_id)


@router.post("/dpdp/requests", status_code=status.HTTP_201_CREATED)
async def raise_request_for(
    payload: RequestForPrincipalIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    """Log a request that arrived off-platform — by post, or to a DPO's inbox."""
    row = await dpdp_service.raise_request(
        session,
        user,
        principal_id=payload.principal_id,
        principal_kind=payload.principal_kind,
        principal_label=payload.principal_label,
        principal_contact=payload.principal_contact,
        request_type=payload.request_type,
        details=payload.details,
        request=request,
    )
    return {"id": str(row.id), "due_by": row.due_by.isoformat()}


@router.get("/dpdp/requests")
async def list_requests(
    session: DbSession,
    user: CurrentUser,
    open_only: bool = False,
    today: date | None = None,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    """Requests with their clocks, and what needs escalating, worst first."""
    return await dpdp_service.list_requests(session, user, open_only=open_only, today=today)


@router.post("/dpdp/requests/{request_id}/resolve")
async def resolve_request(
    request_id: uuid.UUID,
    payload: RequestResolveIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    """Move a request along. A rejection with no reason is refused."""
    row = await dpdp_service.resolve_request(
        session,
        user,
        request_id=request_id,
        new_status=payload.status,
        response_payload=payload.response_payload,
        rejection_reason=payload.rejection_reason,
        note=payload.note,
        request=request,
    )
    return {"id": str(row.id), "status": row.status.value}


@router.get("/dpdp/requests/{request_id}/export")
async def export_for_request(
    request_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    """Build the ACCESS export for a logged request."""
    stmt = tenant_scoped(
        select(DataPrincipalRequest).where(DataPrincipalRequest.id == request_id),
        DataPrincipalRequest,
        user,
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="No such request.")
    if row.principal_kind in dpdp_service.HELD_IN_EMPLOYEE_RECORD:
        raise HTTPException(status_code=400, detail=dpdp_service.HELD_IN_EMPLOYEE_RECORD_NOTE)
    return await dpdp_service.build_access_export(session, user, principal_id=row.principal_id)


@router.get("/dpdp/requests/{request_id}/export/download")
async def download_export_for_request(
    request_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> JSONResponse:
    """The ACCESS export for a logged request, as a file. Admin only, and audited.

    Personal data leaves only to the person or to an administrator answering
    their request — never to HR ops browsing the queue.
    """
    stmt = tenant_scoped(
        select(DataPrincipalRequest).where(DataPrincipalRequest.id == request_id),
        DataPrincipalRequest,
        user,
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="No such request.")
    if row.principal_kind in dpdp_service.HELD_IN_EMPLOYEE_RECORD:
        raise HTTPException(status_code=400, detail=dpdp_service.HELD_IN_EMPLOYEE_RECORD_NOTE)
    document, filename = await dpdp_service.export_file(
        session, user, principal_id=row.principal_id, request=request
    )
    return JSONResponse(
        document, headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


@router.post("/dpdp/requests/{request_id}/execute-erasure")
async def execute_erasure(
    request_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> dict[str, Any]:
    """Carry out an erasure request: purge what may go, keep and explain what must stay.

    Refused while a legal hold covers the person, and refused for your own data.
    """
    return await erasure_service.execute_erasure(
        session, user, request_id=request_id, request=request
    )


@router.get("/dpdp/requests/{request_id}/erasure-preview")
async def erasure_preview_for_request(
    request_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    stmt = tenant_scoped(
        select(DataPrincipalRequest).where(DataPrincipalRequest.id == request_id),
        DataPrincipalRequest,
        user,
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="No such request.")
    if row.principal_kind in dpdp_service.HELD_IN_EMPLOYEE_RECORD:
        raise HTTPException(status_code=400, detail=dpdp_service.HELD_IN_EMPLOYEE_RECORD_NOTE)
    return await dpdp_service.preview_erasure(session, user, principal_id=row.principal_id)


# ── Breach ──────────────────────────────────────────────────────────────────


@router.post("/dpdp/breaches", status_code=status.HTTP_201_CREATED)
async def raise_breach(
    payload: BreachIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> dict[str, Any]:
    """Open an incident. The severity is computed, not chosen.

    Letting whoever files the incident pick its severity puts the grading
    decision on the person under the most pressure to make it small.
    """
    row = await dpdp_service.raise_breach(
        session,
        user,
        reference=payload.reference,
        became_aware_at=payload.became_aware_at,
        detected_at=payload.detected_at,
        description=payload.description,
        affected_data_categories=tuple(payload.affected_data_categories),
        affected_principal_count=payload.affected_principal_count,
        data_left_our_control=payload.data_left_our_control,
        is_rehearsal=payload.is_rehearsal,
        request=request,
    )
    return {
        "id": str(row.id),
        "severity": row.severity.value,
        "board_report_due_at": row.board_report_due_at.isoformat(),
        "is_rehearsal": row.is_rehearsal,
    }


@router.get("/dpdp/breaches")
async def list_breaches(
    session: DbSession,
    user: CurrentUser,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> list[dict[str, Any]]:
    """The breach register: every incident, newest first."""
    return await dpdp_service.list_breaches(session, user)


@router.get("/dpdp/breaches/rehearsal")
async def breach_rehearsal(
    session: DbSession,
    user: CurrentUser,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> dict[str, Any]:
    """A full dry run against a fictional incident. Writes nothing.

    Exists so "have you rehearsed this" has an answer that is not somebody's
    memory of a meeting.
    """
    config = await dpdp_service.require_configuration(session, user)
    return rehearse(
        now=datetime.now(tz=None).astimezone(),
        grievance_contact=config.grievance_contact,
    )


@router.get("/dpdp/breaches/{incident_id}")
async def breach_state(
    incident_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> dict[str, Any]:
    """One incident, both clocks, and what is still owed."""
    return await dpdp_service.breach_state(session, user, incident_id=incident_id)


@router.get("/dpdp/breaches/{incident_id}/notification")
async def breach_notification(
    incident_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> dict[str, Any]:
    """The message affected people would receive. Drafted, not sent."""
    return await dpdp_service.draft_breach_notification(session, user, incident_id=incident_id)


# ── Transfers and the Trust Center ──────────────────────────────────────────


@router.get("/dpdp/sub-processors")
async def sub_processors(user: CurrentUser) -> dict[str, Any]:
    """Where the data goes. Model providers listed first and separately.

    Available to any authenticated user, not gated to HR. An employee asking
    "does an AI company see my performance review" is asking a question they are
    entitled to an answer to, and routing it through their HR team is a way of
    not answering it.
    """
    return transfer_engine.trust_centre_disclosure()


@router.get("/dpdp/transfers")
async def transfer_history(
    session: DbSession,
    user: CurrentUser,
    limit: int = 100,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    """Transfers that actually happened, not the configuration that allows them."""
    return await dpdp_service.transfer_history(session, user, limit=limit)


@router.post("/dpdp/transfers", status_code=status.HTTP_201_CREATED)
async def record_transfer(
    payload: TransferIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> dict[str, Any]:
    """Record a transfer. A category the sub-processor may not receive is refused."""
    row = await dpdp_service.record_transfer(
        session,
        user,
        sub_processor_key=payload.sub_processor_key,
        purpose_key=payload.purpose_key,
        category_keys=tuple(payload.category_keys),
        principal_count=payload.principal_count,
        description=payload.description,
        request=request,
    )
    return {"id": str(row.id), "was_cross_border": row.was_cross_border}


# ── The tenant compliance surface ───────────────────────────────────────────


@router.get("/dpdp/compliance")
async def compliance_surface(
    session: DbSession,
    user: CurrentUser,
    today: date | None = None,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    """Everything a tenant needs to discharge their obligation, on one page.

    Including `known_gaps`, which lists what this module does not yet do. A
    compliance page that only showed the green ticks would be the exact kind of
    invented reassurance this codebase refuses everywhere else.
    """
    return await dpdp_service.tenant_compliance_surface(session, user, today=today)
