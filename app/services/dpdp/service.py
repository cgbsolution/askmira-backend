"""The DB-facing half. Everything decidable lives in the engines beside this.

This module holds sessions, models and transactions and nothing else: no
statutory clocks, no lawful-basis logic, no severity grading. Those are in
`categories`, `consent`, `rights`, `notices`, `breach` and `transfers`, where
they can be read and argued with by somebody who is not going to read a
SQLAlchemy query. An invariant checks the separation holds in the other
direction — none of those modules imports sqlalchemy, app.models or fastapi.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.dpdp import (
    BreachIncident,
    BreachSeverityLevel,
    ConsentCapturedVia,
    ConsentRecord,
    CrossBorderTransfer,
    DataPrincipalKind,
    DataPrincipalRequest,
    DpdpConfiguration,
    DprRequestStatus,
    DprRequestType,
)
from app.models.employee import Employee
from app.models.user import User
from app.services import audit
from app.services.dpdp import breach as breach_engine
from app.services.dpdp import consent as consent_engine
from app.services.dpdp import rights as rights_engine
from app.services.dpdp import transfers as transfer_engine
from app.services.dpdp.categories import CATEGORIES
from app.services.dpdp.contents import WITHHELD_REASON, collect_personal_data
from app.services.dpdp.inventory import (
    build_inventory,
    coverage_gaps,
    lawful_basis_breakdown,
    record_of_processing,
)
from app.services.dpdp.notices import Language, render_notice

__all__ = [
    "capability_permitted",
    "current_schema_shape",
    "list_consent",
    "list_requests",
    "principals_permitting",
    "raise_breach",
    "raise_request",
    "record_consent",
    "record_transfer",
    "resolve_request",
    "tenant_compliance_surface",
]


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def _not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def current_schema_shape() -> dict[str, tuple[str, ...]]:
    """The live schema, as the inventory engine wants it.

    The one place SQLAlchemy metadata is reflected into the DPDP package. The
    engines never see a `Table`, which is what lets them be tested against a
    hand-written five-table fixture.
    """
    from app.models.base import Base

    return {
        table.name: tuple(column.name for column in table.columns)
        for table in Base.metadata.sorted_tables
    }


# ══════════════════════════════════════════════════════════════════════════════
# Configuration
# ══════════════════════════════════════════════════════════════════════════════


async def get_configuration(session: AsyncSession, user: User) -> DpdpConfiguration | None:
    stmt = tenant_scoped(select(DpdpConfiguration), DpdpConfiguration, user)
    return (await session.execute(stmt)).scalar_one_or_none()


async def require_configuration(session: AsyncSession, user: User) -> DpdpConfiguration:
    """The tenant's DPDP settings, or a 422 saying what is missing.

    Refuses rather than defaulting. A grievance contact is the address every
    notice this product renders tells somebody to write to, and a default would
    be a notice pointing at nobody — which is worse than no notice, because it
    looks like a route.
    """
    config = await get_configuration(session, user)
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "This tenant has no DPDP configuration. A grievance contact has "
                "to be set before consent notices can be issued or rights "
                "requests accepted — every notice points a reader at that "
                "address, and there is no sensible default for it."
            ),
        )
    return config


async def upsert_configuration(
    session: AsyncSession,
    user: User,
    *,
    grievance_contact: str,
    data_protection_officer_name: str | None = None,
    sla_days: dict[str, int] | None = None,
    default_notice_language: str = "en",
    ai_processing_enabled: bool = False,
    request: Request | None = None,
) -> DpdpConfiguration:
    if not grievance_contact.strip():
        raise _bad_request("A grievance contact is required — it is printed in every notice.")

    # Validate every SLA against the statutory outer bound before writing. A
    # configuration screen must not be able to commit a tenant to a response
    # period longer than the law allows.
    cleaned: dict[str, int] = {}
    for key, days in (sla_days or {}).items():
        try:
            request_type = rights_engine.RequestType(key)
        except ValueError as exc:
            raise _bad_request(f"{key!r} is not a request type.") from exc
        try:
            cleaned[key] = rights_engine.validate_sla(request_type, days)
        except ValueError as exc:
            raise _bad_request(str(exc)) from exc

    try:
        Language(default_notice_language)
    except ValueError as exc:
        raise _bad_request(
            f"{default_notice_language!r} is not English or an Eighth Schedule language."
        ) from exc

    config = await get_configuration(session, user)
    if config is None:
        config = DpdpConfiguration(tenant_id=user.tenant_id)
        session.add(config)

    config.grievance_contact = grievance_contact.strip()
    config.data_protection_officer_name = data_protection_officer_name
    config.sla_days = cleaned
    config.default_notice_language = default_notice_language
    config.ai_processing_enabled = ai_processing_enabled
    config.configured_by_user_id = user.id

    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="dpdp_configuration",
        entity_id=config.id,
        actor=user,
        summary=f"DPDP configuration set; grievance contact {grievance_contact}.",
        request=request,
    )
    await session.commit()
    await session.refresh(config)
    return config


# ══════════════════════════════════════════════════════════════════════════════
# Consent
# ══════════════════════════════════════════════════════════════════════════════


async def record_consent(
    session: AsyncSession,
    user: User,
    *,
    principal_id: uuid.UUID,
    principal_kind: DataPrincipalKind,
    purpose_key: str,
    granted: bool,
    captured_via: ConsentCapturedVia,
    notice_id: str | None = None,
    notice_language: str = "en",
    request: Request | None = None,
    now: datetime | None = None,
    commit: bool = True,
) -> ConsentRecord:
    """Write one consent decision, refusing where consent is not the basis.

    The refusal comes from the engine, not from here. A caller asking to record
    consent for payroll gets a 400 whose message names the actual lawful basis
    and says to disclose it rather than ask about it — the fix is upstream, in
    whatever screen offered the checkbox.
    """
    moment = now or datetime.now(UTC)

    try:
        decision = consent_engine.record_decision(
            purpose_key,
            granted=granted,
            now=moment,
            captured_via=consent_engine.CapturedVia(captured_via.value),
            ip_address=request.client.host if request and request.client else None,
            notice_id=notice_id,
        )
    except consent_engine.ConsentError as exc:
        raise _bad_request(str(exc)) from exc
    except KeyError as exc:
        raise _bad_request(str(exc.args[0])) from exc

    row = ConsentRecord(
        tenant_id=user.tenant_id,
        principal_id=principal_id,
        principal_kind=principal_kind,
        purpose_key=decision.purpose_key,
        granted=decision.granted,
        decided_at=decision.decided_at,
        purpose_version=decision.version,
        captured_via=captured_via,
        notice_id=notice_id,
        notice_version=1,
        notice_language=notice_language,
        ip_address=decision.ip_address,
        user_agent=request.headers.get("user-agent") if request else None,
        recorded_by_user_id=user.id,
    )
    session.add(row)
    await session.flush()

    if purpose_key == "celebrations":
        # The celebrations calendar reads the per-event sharing flags; My privacy
        # writes them here so the one switch and the calendar cannot disagree.
        await _sync_celebration_flags(session, user, principal_id=principal_id, granted=granted)

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="consent_record",
        entity_id=row.id,
        actor=user,
        summary=(
            f"Consent {'granted' if granted else 'withdrawn'} for "
            f"{purpose_key} by {principal_kind.value} {principal_id}."
        ),
        request=request,
    )
    if commit:
        await session.commit()
        await session.refresh(row)
    return row


async def _sync_celebration_flags(
    session: AsyncSession, user: User, *, principal_id: uuid.UUID, granted: bool
) -> None:
    """Agreeing on My privacy shares both; withdrawing stops both. Scope is kept."""
    from app.models.culture_depth import EventPrivacyPreference

    employee_id = await session.scalar(
        tenant_scoped(select(Employee.id).where(Employee.id == principal_id), Employee, user)
    )
    if employee_id is None:
        return
    pref = await session.scalar(
        tenant_scoped(
            select(EventPrivacyPreference).where(EventPrivacyPreference.employee_id == employee_id),
            EventPrivacyPreference,
            user,
        )
    )
    if pref is None:
        pref = EventPrivacyPreference(
            tenant_id=user.tenant_id, employee_id=employee_id, birthday_scope="team"
        )
        session.add(pref)
    pref.share_birthday = granted
    pref.share_work_anniversary = granted
    await session.flush()


async def record_celebration_choice(
    session: AsyncSession,
    user: User,
    *,
    employee_id: uuid.UUID,
    shares_anything: bool,
    request: Request | None = None,
) -> None:
    """The evidence row for a change made on the celebrations settings screen.

    That screen sets each event separately; the consent log records whether the
    person now shares anything at all, so My privacy and an access export show
    the same answer the calendar acts on. No commit: the caller's transaction.
    """
    decision = consent_engine.record_decision(
        "celebrations",
        granted=shares_anything,
        now=datetime.now(UTC),
        captured_via=consent_engine.CapturedVia.SELF_SERVICE,
        ip_address=request.client.host if request and request.client else None,
    )
    session.add(
        ConsentRecord(
            tenant_id=user.tenant_id,
            principal_id=employee_id,
            principal_kind=DataPrincipalKind.EMPLOYEE,
            purpose_key="celebrations",
            granted=decision.granted,
            decided_at=decision.decided_at,
            purpose_version=decision.version,
            captured_via=ConsentCapturedVia.SELF_SERVICE,
            notice_version=1,
            notice_language="en",
            ip_address=decision.ip_address,
            user_agent=request.headers.get("user-agent") if request else None,
            recorded_by_user_id=user.id,
        )
    )
    await session.flush()


async def _decisions_for(
    session: AsyncSession, user: User, principal_id: uuid.UUID
) -> list[consent_engine.ConsentDecision]:
    stmt = tenant_scoped(
        select(ConsentRecord).where(ConsentRecord.principal_id == principal_id),
        ConsentRecord,
        user,
    ).order_by(ConsentRecord.decided_at)
    rows = (await session.execute(stmt)).scalars().all()
    return [
        consent_engine.ConsentDecision(
            purpose_key=row.purpose_key,
            granted=row.granted,
            decided_at=row.decided_at,
            version=row.purpose_version,
            captured_via=consent_engine.CapturedVia(row.captured_via.value),
            ip_address=row.ip_address,
            notice_id=row.notice_id,
        )
        for row in rows
        # A stored purpose key the catalogue no longer defines is skipped rather
        # than crashing the whole consent view. It is reported separately by
        # `retired_purposes` so it is visible, not silently dropped.
        if row.purpose_key in consent_engine.PURPOSES
    ]


async def capability_permitted(
    session: AsyncSession, user: User, *, principal_id: uuid.UUID, capability: str
) -> bool:
    """Whether one consent-dependent capability may run for one person, now."""
    decisions = await _decisions_for(session, user, principal_id)
    return consent_engine.is_permitted(capability, consent_engine.consent_state(decisions))


async def principals_permitting(
    session: AsyncSession,
    user: User,
    *,
    principal_ids: list[uuid.UUID],
    capability: str,
) -> set[uuid.UUID]:
    """Which of these people currently permit `capability`. One query for all."""
    if not principal_ids:
        return set()
    stmt = tenant_scoped(
        select(ConsentRecord).where(ConsentRecord.principal_id.in_(principal_ids)),
        ConsentRecord,
        user,
    ).order_by(ConsentRecord.decided_at)
    by_principal: dict[uuid.UUID, list[consent_engine.ConsentDecision]] = {
        pid: [] for pid in principal_ids
    }
    for row in (await session.execute(stmt)).scalars().all():
        if row.purpose_key not in consent_engine.PURPOSES:
            continue
        by_principal[row.principal_id].append(
            consent_engine.ConsentDecision(
                purpose_key=row.purpose_key,
                granted=row.granted,
                decided_at=row.decided_at,
                version=row.purpose_version,
                captured_via=consent_engine.CapturedVia(row.captured_via.value),
                ip_address=row.ip_address,
                notice_id=row.notice_id,
            )
        )
    return {
        pid
        for pid, decisions in by_principal.items()
        if consent_engine.is_permitted(capability, consent_engine.consent_state(decisions))
    }


async def list_consent(
    session: AsyncSession,
    user: User,
    *,
    principal_id: uuid.UUID,
    audience: str | None = "employee",
) -> dict[str, Any]:
    """Where every purpose stands for one person.

    Includes the non-consent purposes, marked NOT_APPLICABLE. Showing only the
    checkboxes would answer "what did they agree to" and leave "what happens to
    my data that I did not agree to" unanswered, which is the question somebody
    opening this screen is actually asking.
    """
    decisions = await _decisions_for(session, user, principal_id)
    if not any(d.purpose_key == "celebrations" for d in decisions):
        decisions.extend(await _legacy_celebration_decision(session, user, principal_id))
    wanted = (
        tuple(k for k, p in consent_engine.PURPOSES.items() if p.audience == audience)
        if audience
        else ()
    )
    states = consent_engine.consent_state(decisions, purpose_keys_wanted=wanted)

    stored = tenant_scoped(
        select(ConsentRecord.purpose_key)
        .where(ConsentRecord.principal_id == principal_id)
        .distinct(),
        ConsentRecord,
        user,
    )
    stored_keys = set((await session.execute(stored)).scalars().all())
    retired = sorted(stored_keys - set(consent_engine.PURPOSES))

    return {
        "principal_id": str(principal_id),
        "purposes": [
            state.as_dict()
            | {
                "description": consent_engine.purpose(state.purpose_key).description,
                "withdrawal_erases": list(
                    consent_engine.purpose(state.purpose_key).withdrawal_erases
                ),
                "withdrawal_keeps": list(
                    consent_engine.purpose(state.purpose_key).withdrawal_keeps
                ),
            }
            for state in states
        ],
        "capabilities_permitted": sorted(
            capability
            for capability in consent_engine.CAPABILITIES
            if consent_engine.is_permitted(capability, states)
        ),
        # Consent rows for a purpose we have since retired. Surfaced rather than
        # dropped: they are evidence of something we used to do, and a consent
        # view that hid them would be a consent view that had quietly edited
        # somebody's history.
        "retired_purposes_on_record": retired,
    }


async def _legacy_celebration_decision(
    session: AsyncSession, user: User, principal_id: uuid.UUID
) -> list[consent_engine.ConsentDecision]:
    """A celebrations choice made before the consent log recorded it.

    The sharing flags were set on the celebrations screen before that screen
    wrote a consent row. Read as the decision they are, so My privacy does not
    say "not asked" to somebody who answered.
    """
    from app.models.culture_depth import EventPrivacyPreference

    pref = await session.scalar(
        tenant_scoped(
            select(EventPrivacyPreference).where(
                EventPrivacyPreference.employee_id == principal_id
            ),
            EventPrivacyPreference,
            user,
        )
    )
    if pref is None:
        return []
    return [
        consent_engine.ConsentDecision(
            purpose_key="celebrations",
            granted=bool(pref.share_birthday or pref.share_work_anniversary),
            decided_at=pref.updated_at,
            version=consent_engine.purpose("celebrations").version,
            captured_via=consent_engine.CapturedVia.SELF_SERVICE,
        )
    ]


async def withdraw_consent(
    session: AsyncSession,
    user: User,
    *,
    principal_id: uuid.UUID,
    principal_kind: DataPrincipalKind,
    purpose_key: str,
    request: Request | None = None,
) -> dict[str, Any]:
    """Withdraw, and say exactly what stopped and what did not."""
    try:
        effect = consent_engine.withdrawal_effect(purpose_key)
    except consent_engine.ConsentError as exc:
        raise _bad_request(str(exc)) from exc
    except KeyError as exc:
        raise _bad_request(str(exc.args[0])) from exc

    from app.services.dpdp.withdrawal import erase_on_withdrawal

    row = await record_consent(
        session,
        user,
        principal_id=principal_id,
        principal_kind=principal_kind,
        purpose_key=purpose_key,
        granted=False,
        captured_via=ConsentCapturedVia.SELF_SERVICE,
        request=request,
        commit=False,
    )
    # DPDP s.8(7)(a): what was held only under this consent goes with it, in the
    # same transaction as the withdrawal — never a withdrawal that stuck and a
    # deletion that did not.
    erased = await erase_on_withdrawal(
        session, user, principal_id=principal_id, purpose_key=purpose_key
    )
    if any(erased.values()):
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            action=AuditAction.DELETE,
            entity_type="consent_withdrawal_erasure",
            entity_id=row.id,
            actor=user,
            changes=erased,
            summary=(
                f"Consent for {purpose_key} withdrawn; data held only for it erased: "
                + ", ".join(f"{count} {kind.replace('_', ' ')}" for kind, count in erased.items())
                + "."
            ),
            request=request,
        )
    await session.commit()
    return effect.as_dict() | {"erased_counts": erased}


async def consent_summary(session: AsyncSession, user: User) -> dict[str, Any]:
    """Consent across the workforce, per purpose.

    The number a Data Fiduciary is asked for and rarely has: not "do you collect
    consent" but "for this purpose, how many of your people said yes, how many
    said no, and how many were never asked". The third number is usually the
    surprising one.
    """
    headcount_stmt = tenant_scoped(
        select(func.count()).select_from(Employee), Employee, user
    ).where(Employee.is_active.is_(True))
    headcount = (await session.execute(headcount_stmt)).scalar_one()

    rows_stmt = tenant_scoped(
        select(
            ConsentRecord.purpose_key,
            ConsentRecord.principal_id,
            ConsentRecord.granted,
            ConsentRecord.decided_at,
        ),
        ConsentRecord,
        user,
    ).order_by(ConsentRecord.decided_at)
    rows = (await session.execute(rows_stmt)).all()

    # Latest decision per (purpose, principal).
    latest: dict[tuple[str, uuid.UUID], bool] = {}
    for purpose_key, principal_id, granted, _decided_at in rows:
        latest[(purpose_key, principal_id)] = granted

    out = []
    for spec in consent_engine.consent_purposes("employee"):
        answers = [granted for (key, _pid), granted in latest.items() if key == spec.key]
        yes = sum(1 for a in answers if a)
        no = len(answers) - yes
        out.append(
            {
                "purpose": spec.key,
                "name": spec.name,
                "granted": yes,
                "declined": no,
                "never_asked": max(0, headcount - len(answers)),
                "active_employees": headcount,
            }
        )
    return {"headcount": headcount, "purposes": out}


# ══════════════════════════════════════════════════════════════════════════════
# Notices
# ══════════════════════════════════════════════════════════════════════════════


async def notice_for(
    session: AsyncSession, user: User, *, language: str | None = None
) -> dict[str, Any]:
    config = await get_configuration(session, user)
    try:
        chosen = Language(language or (config.default_notice_language if config else "en"))
    except ValueError as exc:
        raise _bad_request(f"{language!r} is not English or an Eighth Schedule language.") from exc

    notice = render_notice(
        language=chosen,
        notice_id=f"{user.tenant_id}:{chosen.value}:v1",
        # An employee's notice covers what is done with an employee's data; the
        # candidate purposes are in the careers notice, where candidates read it.
        purpose_keys=consent_engine.employee_purpose_keys(),
    )
    # Without a configuration the notice still says what is done and why, and
    # says plainly that no contact has been named — never an invented address.
    return notice.as_dict() | {
        "grievance_contact": config.grievance_contact if config else None,
        "data_protection_officer_name": (config.data_protection_officer_name if config else None),
        "grievance_contact_missing": config is None,
    }


async def configuration_status(session: AsyncSession, user: User) -> dict[str, Any]:
    """Whether this workspace has named its grievance contact — for the admin banner."""
    config = await get_configuration(session, user)
    return {
        "configured": config is not None,
        "grievance_contact": config.grievance_contact if config else None,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Data-principal requests
# ══════════════════════════════════════════════════════════════════════════════


async def raise_request(
    session: AsyncSession,
    user: User,
    *,
    principal_id: uuid.UUID,
    principal_kind: DataPrincipalKind,
    principal_label: str,
    request_type: DprRequestType,
    details: str | None = None,
    principal_contact: str | None = None,
    request: Request | None = None,
    now: datetime | None = None,
) -> DataPrincipalRequest:
    """Accept a request and start its clock.

    The clock starts on receipt, not on identity verification. Verifying who
    somebody is is our problem; treating it as a pause button is how a 30-day
    response period becomes a 30-day response period plus however long we take.
    """
    # A right is never refused because the workspace has not finished its
    # privacy setup: without a configuration the statutory default periods
    # apply, and the admins see a banner until a grievance contact is set.
    config = await get_configuration(session, user)
    moment = now or datetime.now(UTC)

    engine_type = rights_engine.RequestType(request_type.value)
    clock = rights_engine.clock_for(
        engine_type,
        received_on=moment.date(),
        sla_days=(config.sla_days or {}).get(request_type.value) if config else None,
    )

    row = DataPrincipalRequest(
        tenant_id=user.tenant_id,
        principal_id=principal_id,
        principal_kind=principal_kind,
        principal_label=principal_label,
        principal_contact=principal_contact,
        request_type=request_type,
        status=DprRequestStatus.RECEIVED,
        details=details,
        received_at=moment,
        due_by=clock.due_by,
        escalate_at=clock.escalate_at,
        sla_days=clock.sla_days,
        actions=[
            {
                "at": moment.isoformat(),
                "actor": str(user.id),
                "action": "received",
                "note": f"Due by {clock.due_by.isoformat()}.",
            }
        ],
    )
    session.add(row)
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="data_principal_request",
        entity_id=row.id,
        actor=user,
        summary=(
            f"{request_type.value} request from {principal_label}, due {clock.due_by.isoformat()}."
        ),
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return row


async def list_requests(
    session: AsyncSession,
    user: User,
    *,
    open_only: bool = False,
    today: date | None = None,
) -> dict[str, Any]:
    """Open requests with their clocks, and what needs escalating."""
    stmt = tenant_scoped(select(DataPrincipalRequest), DataPrincipalRequest, user)
    if open_only:
        stmt = stmt.where(
            DataPrincipalRequest.status.in_(
                [
                    DprRequestStatus.RECEIVED,
                    DprRequestStatus.VERIFYING_IDENTITY,
                    DprRequestStatus.IN_PROGRESS,
                    DprRequestStatus.ESCALATED,
                ]
            )
        )
    rows = (await session.execute(stmt.order_by(DataPrincipalRequest.due_by))).scalars().all()
    as_of = today or datetime.now(UTC).date()

    open_clocks: list[tuple[str, rights_engine.RequestClock]] = []
    payload = []
    for row in rows:
        clock = rights_engine.RequestClock(
            request_type=rights_engine.RequestType(row.request_type.value),
            received_on=row.received_at.date(),
            due_by=row.due_by,
            escalate_at=row.escalate_at,
            sla_days=row.sla_days,
            statutory_bound_days=rights_engine.STATUTORY_OUTER_BOUND_DAYS[
                rights_engine.RequestType(row.request_type.value)
            ],
        )
        status_engine = rights_engine.RequestStatus(row.status.value)
        if status_engine.is_open:
            open_clocks.append((str(row.id), clock))
        payload.append(
            {
                "id": str(row.id),
                "principal": row.principal_label,
                "principal_kind": row.principal_kind.value,
                "type": row.request_type.value,
                "status": row.status.value,
                "details": row.details,
                "responded_at": (row.responded_at.isoformat() if row.responded_at else None),
                "rejection_reason": row.rejection_reason,
                "clock": clock.as_dict(today=as_of),
            }
        )

    return {
        "requests": payload,
        "escalations": [e.as_dict() for e in rights_engine.escalations(open_clocks, today=as_of)],
    }


async def _principal_identities(
    session: AsyncSession, user: User, principal_id: uuid.UUID
) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    """The employee record and the login a principal id refers to, in this tenant."""
    employee = await session.scalar(
        tenant_scoped(select(Employee).where(Employee.id == principal_id), Employee, user)
    )
    login = await session.scalar(
        tenant_scoped(
            select(User).where(or_(User.employee_id == principal_id, User.id == principal_id)),
            User,
            user,
        )
    )
    return (employee.id if employee else None, login.id if login else None)


async def _candidate_id(
    session: AsyncSession, user: User, principal_id: uuid.UUID
) -> uuid.UUID | None:
    """The candidate a principal id refers to, in this tenant, if it is one."""
    from app.models.hiring import Candidate

    return await session.scalar(
        tenant_scoped(select(Candidate.id).where(Candidate.id == principal_id), Candidate, user)
    )


#: People whose details live inside an employee's record — a nominee, a family
#: member, an emergency contact. They have no record of their own to export or
#: erase; their request is honoured by correcting the employee's record.
HELD_IN_EMPLOYEE_RECORD = frozenset(
    {
        DataPrincipalKind.DEPENDENT,
        DataPrincipalKind.EMERGENCY_CONTACT,
        DataPrincipalKind.THIRD_PARTY,
    }
)

HELD_IN_EMPLOYEE_RECORD_NOTE = (
    "This person's details are held inside an employee's record — as a nominee, a "
    "family member or an emergency contact — not in a record of their own. To answer: "
    "open that employee's profile, show or correct or remove the entries about this "
    "person (Family, Nominees, Emergency contacts), then mark this request completed "
    "with what was done."
)


async def export_file(
    session: AsyncSession,
    user: User,
    *,
    principal_id: uuid.UUID,
    request: Request | None = None,
) -> tuple[dict[str, Any], str]:
    """The access export as a file, audited before it leaves.

    The caller decides who may ask: the person themselves, or an administrator
    acting on a logged request. The audit row names which tables left.
    """
    document = await build_access_export(session, user, principal_id=principal_id)
    completeness = document["completeness"]
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.EXPORT,
        entity_type="personal_data_export",
        entity_id=principal_id,
        actor=user,
        summary=(
            f"Personal data export downloaded: {completeness['rows_exported']} row(s) "
            f"across {completeness['tables_with_your_rows']} table(s)."
        ),
        changes={
            "tables": [section["table"] for section in document["contents"]],
            "for_self": principal_id in {user.employee_id, user.id},
        },
        request=request,
    )
    await session.commit()
    return document, f"askmira-personal-data-{datetime.now(UTC):%Y%m%d}.json"


async def list_my_requests(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    """Rights requests about the caller, newest first, with their outcomes."""
    own = [pid for pid in (user.employee_id, user.id) if pid is not None]
    rows = (
        await session.scalars(
            tenant_scoped(
                select(DataPrincipalRequest).where(DataPrincipalRequest.principal_id.in_(own)),
                DataPrincipalRequest,
                user,
            ).order_by(DataPrincipalRequest.received_at.desc())
        )
    ).all()
    return [
        {
            "id": str(row.id),
            "type": row.request_type.value,
            "status": row.status.value,
            "details": row.details,
            "received_at": row.received_at.isoformat(),
            "due_by": row.due_by.isoformat(),
            "responded_at": row.responded_at.isoformat() if row.responded_at else None,
            "rejection_reason": row.rejection_reason,
            "response": row.response_payload or {},
        }
        for row in rows
    ]


async def build_access_export(
    session: AsyncSession, user: User, *, principal_id: uuid.UUID
) -> dict[str, Any]:
    """A readable package of everything held about one person.

    Organised by data category, in catalogue order, with the plain-language
    description of each and where it came from — and then the rows themselves,
    table by table, from every classified table linked directly to the person's
    employee record or login (`contents.collect_personal_data`). Credential
    material is withheld and the export says so.
    """
    inventory = build_inventory(current_schema_shape())
    grouped = inventory.by_category()

    sections = []
    for cat in inventory.categories_present:
        columns = grouped.get(cat.key, [])
        sections.append(
            {
                "category": cat.key,
                "name": cat.name,
                "what_this_is": cat.description,
                "why_we_hold_it": cat.lawful_basis.label,
                "how_long": (
                    f"{cat.retention.months} months from "
                    f"{cat.retention.clock_starts.replace('_', ' ')}"
                    + (
                        f", required by {cat.retention.statutory_reference}"
                        if cat.retention.is_statutory
                        else " (our policy, not a legal requirement)"
                    )
                ),
                "held_in": sorted({c.table for c in columns}),
                "field_count": len(columns),
            }
        )

    employee_id, login_id = await _principal_identities(session, user, principal_id)
    candidate_id = (
        await _candidate_id(session, user, principal_id)
        if employee_id is None and login_id is None
        else None
    )
    contents = await collect_personal_data(
        session, user, employee_id=employee_id, user_id=login_id, candidate_id=candidate_id
    )

    return {
        "principal_id": str(principal_id),
        "generated_at": datetime.now(UTC).isoformat(),
        "sections": sections,
        "consent": await list_consent(session, user, principal_id=principal_id),
        "contents": contents,
        "completeness": {
            "categories_covered": len(sections),
            "modules_covered": inventory.tables_holding_personal_data,
            "contents_included": True,
            "tables_with_your_rows": len(contents),
            "rows_exported": sum(section["row_count"] for section in contents),
            "truncated_tables": [s["table"] for s in contents if s["truncated"]],
            "withheld": WITHHELD_REASON,
            "note": (
                "Every row held about you in a table linked directly to your employee "
                "record or your login is included under `contents`. A record reached "
                "only through another one — a line of a settlement, say — is covered "
                "by its parent. Credential material is withheld; see `withheld`."
            ),
        },
    }


async def preview_erasure(
    session: AsyncSession,
    user: User,
    *,
    principal_id: uuid.UUID,
    legal_holds: tuple[str, ...] = (),
    today: date | None = None,
) -> dict[str, Any]:
    """What an erasure request would and would not delete, before doing it.

    Shown to the person who asked as well as to whoever will action it. An
    erasure whose partial nature is only discovered after the fact is an
    erasure the requester was misled about.
    """
    plan = await erasure_plan_for(
        session, user, principal_id=principal_id, legal_holds=legal_holds, today=today
    )
    return plan.as_dict()


async def erasure_plan_for(
    session: AsyncSession,
    user: User,
    *,
    principal_id: uuid.UUID,
    legal_holds: tuple[str, ...] = (),
    today: date | None = None,
) -> rights_engine.ErasurePlan:
    """The category-by-category erasure decision for one person in this tenant."""
    as_of = today or datetime.now(UTC).date()

    employee = await session.scalar(
        tenant_scoped(select(Employee).where(Employee.id == principal_id), Employee, user)
    )
    last_working_day = employee.date_of_exit if employee else None

    inventory = build_inventory(current_schema_shape())
    held = tuple(dict.fromkeys(c.category_key for c in inventory.columns))

    try:
        return rights_engine.reconcile_erasure(
            held,
            requested_on=as_of,
            last_working_day=last_working_day,
            financial_year_end=_financial_year_end(last_working_day or as_of),
            legal_holds=frozenset(legal_holds),
        )
    except rights_engine.ReconciliationError as exc:
        raise _bad_request(str(exc)) from exc


def _financial_year_end(reference: date) -> date:
    """31 March of the financial year the reference date falls in.

    India's financial year runs April to March, and retention clocks that start
    "at the end of the financial year" start here. Using 31 December would
    release every record three months early.
    """
    year = reference.year if reference.month > 3 else reference.year - 1
    return date(year + 1, 3, 31)


async def resolve_request(
    session: AsyncSession,
    user: User,
    *,
    request_id: uuid.UUID,
    new_status: DprRequestStatus,
    response_payload: dict[str, Any] | None = None,
    rejection_reason: str | None = None,
    note: str | None = None,
    request: Request | None = None,
    now: datetime | None = None,
) -> DataPrincipalRequest:
    """Move a request along, refusing a refusal with no reason.

    DPDP does not forbid refusing a request; it forbids refusing one without
    telling the person why. So a REJECTED status with no `rejection_reason` is a
    400 here, not a nullable column somebody forgets to fill.
    """
    stmt = tenant_scoped(
        select(DataPrincipalRequest).where(DataPrincipalRequest.id == request_id),
        DataPrincipalRequest,
        user,
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise _not_found("No such data-principal request.")

    if new_status is DprRequestStatus.REJECTED and not (rejection_reason or "").strip():
        raise _bad_request(
            "A rejected request has to say why. Refusing a data principal's "
            "request is permitted; refusing it without a reason they can read "
            "is not."
        )
    if new_status is DprRequestStatus.PARTIALLY_COMPLETED and not response_payload:
        raise _bad_request(
            "A partially completed request has to carry the response payload "
            "saying which parts were honoured and which were not, and under "
            "what obligation. 'Partially' with no detail tells the person "
            "nothing."
        )

    moment = now or datetime.now(UTC)
    row.status = new_status
    if response_payload is not None:
        row.response_payload = response_payload
    if rejection_reason is not None:
        row.rejection_reason = rejection_reason
    if new_status in (
        DprRequestStatus.COMPLETED,
        DprRequestStatus.PARTIALLY_COMPLETED,
        DprRequestStatus.REJECTED,
    ):
        row.responded_at = moment

    # New list, not an append to the existing one. A JSONB column assigned a
    # list that was mutated in place is not flagged dirty and never written —
    # the same failure that made a lifecycle approval reassignment report
    # success and store nothing.
    row.actions = [
        *(row.actions or []),
        {
            "at": moment.isoformat(),
            "actor": str(user.id),
            "action": new_status.value,
            "note": note or "",
        },
    ]

    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="data_principal_request",
        entity_id=row.id,
        actor=user,
        summary=f"Request {request_id} moved to {new_status.value}.",
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return row


# ══════════════════════════════════════════════════════════════════════════════
# Breach
# ══════════════════════════════════════════════════════════════════════════════


async def raise_breach(
    session: AsyncSession,
    user: User,
    *,
    reference: str,
    became_aware_at: datetime,
    description: str,
    affected_data_categories: tuple[str, ...],
    affected_principal_count: int,
    data_left_our_control: bool,
    detected_at: datetime | None = None,
    is_rehearsal: bool = False,
    request: Request | None = None,
) -> BreachIncident:
    """Open an incident, graded and clocked.

    The severity is computed, not chosen. Letting whoever files the incident
    pick the severity puts the grading decision on the person under the most
    pressure to make it small.
    """
    try:
        severity = breach_engine.assess_severity(
            category_keys=affected_data_categories,
            affected_principal_count=affected_principal_count,
            data_left_our_control=data_left_our_control,
        )
    except breach_engine.BreachError as exc:
        raise _bad_request(str(exc)) from exc

    clock = breach_engine.clock_from(became_aware_at)

    row = BreachIncident(
        tenant_id=user.tenant_id,
        reference=reference,
        became_aware_at=became_aware_at,
        detected_at=detected_at,
        description=description,
        affected_data_categories=list(affected_data_categories),
        affected_principal_count=affected_principal_count,
        data_left_our_control=data_left_our_control,
        severity=BreachSeverityLevel(severity.value),
        board_report_due_at=clock.board_report_due_at,
        is_rehearsal=is_rehearsal,
        raised_by_user_id=user.id,
    )
    session.add(row)
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="breach_incident",
        entity_id=row.id,
        actor=user,
        summary=(
            f"{'REHEARSAL: ' if is_rehearsal else ''}Breach {reference} graded "
            f"{severity.value}; Board report due "
            f"{clock.board_report_due_at.isoformat()}."
        ),
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return row


async def list_breaches(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    """Every incident in this workspace, newest first, rehearsals included.

    A summary per incident; `breach_state` is the full view with both clocks.
    Without a list, an incident raised last week is reachable only by someone
    who kept its id, which is not a register.
    """
    stmt = tenant_scoped(
        select(BreachIncident).order_by(BreachIncident.became_aware_at.desc()),
        BreachIncident,
        user,
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [
        {
            "id": str(row.id),
            "reference": row.reference,
            "is_rehearsal": row.is_rehearsal,
            "severity": row.severity.value,
            "became_aware_at": row.became_aware_at.isoformat(),
            "affected_principal_count": row.affected_principal_count,
            "board_report_due_at": row.board_report_due_at.isoformat(),
            "reported_to_board_at": (
                row.reported_to_board_at.isoformat() if row.reported_to_board_at else None
            ),
            "notification_sent": row.notification_sent,
        }
        for row in rows
    ]


async def breach_state(
    session: AsyncSession, user: User, *, incident_id: uuid.UUID, now: datetime | None = None
) -> dict[str, Any]:
    """One incident with both clocks and what is still owed."""
    stmt = tenant_scoped(
        select(BreachIncident).where(BreachIncident.id == incident_id),
        BreachIncident,
        user,
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise _not_found("No such breach incident.")

    moment = now or datetime.now(UTC)
    clock = breach_engine.clock_from(row.became_aware_at)
    severity = breach_engine.BreachSeverity(row.severity.value)
    obligations = breach_engine.outstanding_obligations(
        clock=clock,
        severity=severity,
        principals_notified_at=row.principals_notified_at,
        board_reported_at=row.reported_to_board_at,
        now=moment,
    )

    return {
        "id": str(row.id),
        "reference": row.reference,
        "is_rehearsal": row.is_rehearsal,
        "severity": row.severity.value,
        "description": row.description,
        "affected_data_categories": row.affected_data_categories,
        "affected_principal_count": row.affected_principal_count,
        "clock": clock.as_dict(now=moment),
        "obligations": [o.as_dict() for o in obligations],
        "remediation": row.remediation,
        "notification_sent": row.notification_sent,
    }


async def draft_breach_notification(
    session: AsyncSession, user: User, *, incident_id: uuid.UUID
) -> dict[str, Any]:
    config = await require_configuration(session, user)
    stmt = tenant_scoped(
        select(BreachIncident).where(BreachIncident.id == incident_id),
        BreachIncident,
        user,
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise _not_found("No such breach incident.")

    try:
        draft = breach_engine.notification_to_principals(
            category_keys=tuple(row.affected_data_categories),
            severity=breach_engine.BreachSeverity(row.severity.value),
            what_happened=row.description,
            became_aware_at=row.became_aware_at,
            remediation=row.remediation or "",
            grievance_contact=config.grievance_contact,
        )
    except breach_engine.BreachError as exc:
        raise _bad_request(str(exc)) from exc
    return draft.as_dict()


# ══════════════════════════════════════════════════════════════════════════════
# Cross-border transfers
# ══════════════════════════════════════════════════════════════════════════════


async def record_transfer(
    session: AsyncSession,
    user: User,
    *,
    sub_processor_key: str,
    purpose_key: str,
    category_keys: tuple[str, ...],
    principal_count: int,
    description: str,
    request: Request | None = None,
    now: datetime | None = None,
    commit: bool = True,
) -> CrossBorderTransfer:
    """Record a transfer, refusing one the sub-processor may not receive.

    `commit=False` for a caller inside its own transaction — the agent layer
    will record a transfer as part of the same unit of work as the AI call it
    describes, and a commit here would close that out from under it.
    """
    try:
        transfer_engine.assert_transfer_allowed(sub_processor_key, category_keys)
    except transfer_engine.TransferError as exc:
        raise _bad_request(str(exc)) from exc
    except KeyError as exc:
        raise _bad_request(str(exc.args[0])) from exc

    row = CrossBorderTransfer(
        tenant_id=user.tenant_id,
        sub_processor_key=sub_processor_key,
        purpose_key=purpose_key,
        data_categories=list(category_keys),
        principal_count=principal_count,
        occurred_at=now or datetime.now(UTC),
        description=description,
        was_cross_border=transfer_engine.crosses_border(sub_processor_key),
        initiated_by_user_id=user.id,
    )
    session.add(row)
    await session.flush()

    if commit:
        await session.commit()
        await session.refresh(row)
    return row


async def transfer_history(
    session: AsyncSession, user: User, *, limit: int = 100
) -> dict[str, Any]:
    stmt = (
        tenant_scoped(select(CrossBorderTransfer), CrossBorderTransfer, user)
        .order_by(CrossBorderTransfer.occurred_at.desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).scalars().all()

    count_stmt = tenant_scoped(
        select(func.count()).select_from(CrossBorderTransfer),
        CrossBorderTransfer,
        user,
    ).where(CrossBorderTransfer.was_cross_border.is_(True))
    cross_border_total = (await session.execute(count_stmt)).scalar_one()

    return {
        "disclosure": transfer_engine.trust_centre_disclosure(),
        "cross_border_transfers_recorded": cross_border_total,
        "recent": [
            {
                "sub_processor": row.sub_processor_key,
                "purpose": row.purpose_key,
                "categories": row.data_categories,
                "principal_count": row.principal_count,
                "occurred_at": row.occurred_at.isoformat(),
                "was_cross_border": row.was_cross_border,
                "description": row.description,
            }
            for row in rows
        ],
    }


# ══════════════════════════════════════════════════════════════════════════════
# The tenant-facing surface
# ══════════════════════════════════════════════════════════════════════════════


async def tenant_compliance_surface(
    session: AsyncSession, user: User, *, today: date | None = None
) -> dict[str, Any]:
    """Everything a tenant needs to discharge their obligation, on one page.

    They are the Data Fiduciary and we are the Data Processor, which means their
    compliance is discharged through this product. The processing record, the
    consent position across their workforce, the open requests with their due
    dates and the breach history — all generated, none of it a document
    somebody maintains.

    `is_rehearsal` incidents are excluded from the count and reported
    separately. A rehearsal in a breach history shown to a regulator would be a
    breach we did not have.
    """
    schema = current_schema_shape()
    as_of = today or datetime.now(UTC).date()

    config = await get_configuration(session, user)
    requests = await list_requests(session, user, open_only=True, today=as_of)

    real_breaches = tenant_scoped(
        select(func.count()).select_from(BreachIncident), BreachIncident, user
    ).where(BreachIncident.is_rehearsal.is_(False))
    rehearsals = tenant_scoped(
        select(func.count()).select_from(BreachIncident), BreachIncident, user
    ).where(BreachIncident.is_rehearsal.is_(True))

    return {
        "roles": {
            "data_fiduciary": "The tenant. They decide why and how data is processed.",
            "data_processor": "AskMira. We process it on their instructions.",
            "note": (
                "The obligations under the Act sit with the Data Fiduciary. This "
                "page is how they are discharged through this product; it is not "
                "a transfer of them to us."
            ),
        },
        "configured": config is not None,
        "grievance_contact": config.grievance_contact if config else None,
        "record_of_processing": [a.as_dict() for a in record_of_processing(schema)],
        "lawful_basis_breakdown": lawful_basis_breakdown(schema),
        "inventory": build_inventory(schema).as_dict(),
        "classification_gaps": [str(g) for g in coverage_gaps(schema)],
        "consent": await consent_summary(session, user),
        "open_requests": requests,
        "breach_history": {
            "incidents": (await session.execute(real_breaches)).scalar_one(),
            "rehearsals": (await session.execute(rehearsals)).scalar_one(),
        },
        "sub_processors": transfer_engine.trust_centre_disclosure(),
        "known_gaps": [
            "The access export covers tables linked directly to the person's "
            "employee record, login or candidate record; child rows are covered "
            "through their parent. Nominees, family members and emergency contacts "
            "have no record of their own: their requests are answered on the "
            "employee's record.",
            "A deleted workspace's data is purged 90 days after deletion, files "
            "included, except its audit trail, which is insert-only by design and "
            "kept as the record of what was done.",
            "Erasure is executed by an administrator on an approved request. It does "
            "not reach database backups: deleted rows are suppressed if a backup is "
            "restored, and the audit trail is never pseudonymised.",
            "Notices ship in English and Hindi only; other Eighth Schedule "
            "languages fall back to English and say so.",
            "Every AI call records its transfer to the model provider, but the "
            "provider's zero-data-retention and processing agreement are settings on "
            "the API account that AskMira cannot verify from here.",
            "The lawful-basis mapping has not been reviewed by counsel.",
        ],
    }


def category_catalogue() -> list[dict[str, Any]]:
    return [c.as_dict() for c in CATEGORIES.values()]
