"""Operational security endpoints (slice 30).

`GET /opsec/posture` is the one a procurement questionnaire is answered from:
recovery objectives with the evidence behind them, session and access controls,
the pen-test summary, the SIEM schema — and a `not_built` list on every section.

A questionnaire answered from a half-wired feature is worse than one answered
honestly, because the client verifies it later, usually during an incident.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, Field

from app.core.deps import CurrentUser, DbSession, require_roles
from app.core.security import verify_password
from app.models.opsec import (
    DataTierEnum,
    FindingSeverityEnum,
    SiemVendorEnum,
)
from app.models.user import UserRole
from app.services.opsec import access, continuity, pentest, siem
from app.services.opsec import service as opsec

router = APIRouter(tags=["opsec"])

HR_ROLES = (UserRole.TENANT_ADMIN, UserRole.HR_ADMIN, UserRole.HR_OPS)
ADMIN_ROLES = (UserRole.TENANT_ADMIN, UserRole.HR_ADMIN)


# ── Contracts ───────────────────────────────────────────────────────────────


class SessionPolicyIn(BaseModel):
    idle_timeout_minutes: int = Field(ge=1, le=10080)
    absolute_lifetime_hours: int = Field(ge=1, le=720)
    #: Zero means unlimited. A consultant on a laptop and a phone is not an
    #: attack, so a limit is opt-in.
    max_concurrent_sessions: int = Field(default=0, ge=0, le=100)
    enforce_ip_allowlist_for_privileged: bool = False
    #: CIDR ranges. **Empty permits everything** — unconfigured and deny-all are
    #: different states, and a tenant must not meet this feature by being locked
    #: out of their own payroll.
    ip_allowlist: list[str] = Field(default_factory=list)


class RevokeIn(BaseModel):
    session_id: uuid.UUID | None = None
    all_for_user: uuid.UUID | None = None
    reason: str = Field(default="Signed out by a person.", max_length=160)


class StepUpIn(BaseModel):
    action_key: str = Field(min_length=1, max_length=96)
    session_id: uuid.UUID | None = None


class SatisfyIn(BaseModel):
    """The account password, re-entered. Checked here; never stored or logged."""

    password: str = Field(min_length=1, max_length=512)


class DestinationIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    vendor: SiemVendorEnum
    endpoint_url: str = Field(min_length=1, max_length=500)
    #: How long a gap is normal for this tenant. A quiet tenant legitimately
    #: goes hours between events, and alerting on a normal quiet period is how
    #: the alert gets muted.
    expected_interval_minutes: int = Field(default=1440, ge=1)
    datasets: list[str] = Field(default_factory=list)


class DeliveryIn(BaseModel):
    destination_id: uuid.UUID
    succeeded: bool
    event_count: int = Field(default=0, ge=0)
    error: str | None = None
    latency_ms: int | None = Field(default=None, ge=0)


class DrillIn(BaseModel):
    tier: DataTierEnum
    performed_on: date
    #: Measured, not estimated. A drill whose duration was estimated is a
    #: rehearsal of the paperwork.
    measured_rto_minutes: int = Field(ge=0)
    measured_rpo_minutes: int = Field(ge=0)
    succeeded: bool
    notes: str = Field(default="", max_length=4000)


class FindingIn(BaseModel):
    reference: str = Field(min_length=1, max_length=40)
    title: str = Field(min_length=1, max_length=300)
    severity: FindingSeverityEnum
    area: str = Field(min_length=1, max_length=48)
    discovered_on: date
    tester: str | None = Field(default=None, max_length=160)


class AllowlistCheckIn(BaseModel):
    address: str = Field(min_length=1, max_length=64)
    break_glass_active: bool = False


# ── Posture ─────────────────────────────────────────────────────────────────


@router.get("/opsec/posture")
async def posture(
    session: DbSession,
    user: CurrentUser,
    today: date | None = None,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    """What a procurement questionnaire asks, answered honestly.

    Every published objective carries whether it is currently evidenced by a
    passing drill, and every section lists what is not built.
    """
    return await opsec.posture(session, user, today=today)


@router.get("/opsec/continuity")
async def continuity_posture(user: CurrentUser) -> dict[str, Any]:
    """Recovery objectives, runbooks and severity definitions.

    Not role-gated. An employee asking what happens to their payslip during an
    outage is asking a reasonable question, and the answer is not a secret.
    """
    return continuity.published_posture([], today=date.today())


# ── Sessions and access ─────────────────────────────────────────────────────


@router.get("/opsec/sessions")
async def list_sessions(
    session: DbSession,
    user: CurrentUser,
    target_user_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Your active sessions, with the device and where from.

    Not role-gated for your own sessions: seeing where your account is signed in
    and killing one is something you should not need HR's help for. Listing
    somebody else's needs an admin, checked below.
    """
    if target_user_id is not None and user.role not in ADMIN_ROLES:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=403,
            detail="Only an admin can list another person's sessions.",
        )
    return await opsec.list_sessions(
        session, user, target_user_id=target_user_id
    )


@router.get("/opsec/sessions/policy")
async def get_session_policy(
    session: DbSession,
    user: CurrentUser,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> dict[str, Any]:
    """The saved policy, including the allowlist, or the defaults if none is saved."""
    return await opsec.policy_settings(session, user)


@router.get("/opsec/sessions/active")
async def all_active_sessions(
    session: DbSession,
    user: CurrentUser,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> list[dict[str, Any]]:
    """Every tracked session in the workspace. Tracking starts once a policy is saved."""
    return await opsec.active_sessions_everyone(session, user)


@router.get("/opsec/registers")
async def registers(
    session: DbSession,
    user: CurrentUser,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> dict[str, Any]:
    """Restore drills, pen-test findings and SIEM destinations, as recorded."""
    return await opsec.registers(session, user)


@router.put("/opsec/sessions/policy")
async def set_session_policy(
    payload: SessionPolicyIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> dict[str, Any]:
    """Set the tenant's session rules.

    An idle timeout longer than the absolute lifetime can never fire, so it is
    refused rather than stored as a setting that does nothing.
    """
    row = await opsec.configure_sessions(
        session,
        user,
        idle_timeout_minutes=payload.idle_timeout_minutes,
        absolute_lifetime_hours=payload.absolute_lifetime_hours,
        max_concurrent_sessions=payload.max_concurrent_sessions,
        enforce_ip_allowlist_for_privileged=(
            payload.enforce_ip_allowlist_for_privileged
        ),
        ip_allowlist=payload.ip_allowlist,
        request=request,
    )
    return {
        "id": str(row.id),
        "idle_timeout_minutes": row.idle_timeout_minutes,
        "absolute_lifetime_hours": row.absolute_lifetime_hours,
        "max_concurrent_sessions": row.max_concurrent_sessions,
        "ip_allowlist_entries": len(row.ip_allowlist or []),
    }


@router.post("/opsec/sessions/revoke")
async def revoke(
    payload: RevokeIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
) -> dict[str, Any]:
    """End one session, or every session for a user.

    Revoking all bumps the token validity floor, which is what makes it
    immediate — the auth layer checks that on every request rather than waiting
    for each session to go idle.
    """
    if payload.all_for_user is not None and user.role not in ADMIN_ROLES:
        from fastapi import HTTPException

        if payload.all_for_user != user.id:
            raise HTTPException(
                status_code=403,
                detail="Only an admin can sign somebody else out everywhere.",
            )
    return await opsec.revoke_session(
        session,
        user,
        session_id=payload.session_id,
        all_for_user=payload.all_for_user,
        reason=payload.reason,
        request=request,
    )


@router.post("/opsec/step-up")
async def require_step_up(
    payload: StepUpIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
) -> dict[str, Any]:
    """Whether this action needs fresh proof, and open a challenge if so.

    Keyed on the action and on how recently the person proved who they are —
    never on session age. An attacker who signed in ten minutes ago with stolen
    credentials has a very fresh session.
    """
    return await opsec.require_step_up(
        session,
        user,
        action_key=payload.action_key,
        session_id=payload.session_id,
        ip_address=request.client.host if request.client else None,
    )


@router.post("/opsec/step-up/{challenge_id}")
async def satisfy_step_up(
    challenge_id: uuid.UUID,
    payload: SatisfyIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
) -> dict[str, Any]:
    """Record the outcome of a challenge. Failures are recorded too.

    A run of failed challenges against a disbursement release is exactly the
    signal a SOC wants, and it only exists if the failures are stored.
    """
    result = await opsec.satisfy_challenge(
        session,
        user,
        challenge_id=challenge_id,
        succeeded=verify_password(payload.password, user.password_hash),
        request=request,
    )
    result["not_built"] = (
        "The account password was re-checked. No second factor (an authenticator "
        "app or a device) exists yet."
    )
    return result


@router.get("/opsec/step-up/actions")
async def step_up_actions(user: CurrentUser) -> dict[str, Any]:
    """Which actions need fresh proof, and why each one does."""
    return {
        "actions": [
            {"action_key": key, "reason": access.step_up_reason(key)}
            for key in sorted(access.STEP_UP_ACTIONS)
        ],
        "window_minutes": int(access.STEP_UP_WINDOW.total_seconds() // 60),
        "statement": access.posture_summary()["step_up_statement"],
    }


@router.post("/opsec/allowlist/check")
async def check_allowlist(
    payload: AllowlistCheckIn,
    session: DbSession,
    user: CurrentUser,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> dict[str, Any]:
    """Test an address against this tenant's allowlist before relying on it.

    Exists so an admin can check their office range *before* going home, rather
    than discovering the answer by being locked out on the 30th.
    """
    from sqlalchemy import select

    from app.core.deps import tenant_scoped
    from app.models.opsec import TenantSessionPolicy

    row = (
        await session.execute(
            tenant_scoped(select(TenantSessionPolicy), TenantSessionPolicy, user)
        )
    ).scalar_one_or_none()
    entries = tuple(row.ip_allowlist or []) if row else ()

    decision = access.check_allowlist(
        address=payload.address,
        entries=entries,
        break_glass_active=payload.break_glass_active,
    )
    return decision.as_dict()


# ── SIEM ────────────────────────────────────────────────────────────────────


@router.get("/opsec/siem/schema")
async def siem_schema(user: CurrentUser) -> dict[str, Any]:
    """The event schema, documented, so a client can build a parser.

    Published rather than described on a call: every field, its ECS name, and
    what we put in it.
    """
    return siem.schema_documentation()


@router.put("/opsec/siem/destinations", status_code=status.HTTP_201_CREATED)
async def configure_destination(
    payload: DestinationIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> dict[str, Any]:
    row = await opsec.configure_destination(
        session,
        user,
        name=payload.name,
        vendor=payload.vendor,
        endpoint_url=payload.endpoint_url,
        expected_interval_minutes=payload.expected_interval_minutes,
        datasets=payload.datasets,
        request=request,
    )
    return {
        "id": str(row.id),
        "name": row.name,
        "vendor": row.vendor.value,
        "health_state": row.health_state.value,
    }


@router.post("/opsec/siem/deliveries", status_code=status.HTTP_201_CREATED)
async def record_delivery(
    payload: DeliveryIn,
    session: DbSession,
    user: CurrentUser,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> dict[str, Any]:
    """Record one delivery attempt, successful or not.

    Successes matter: health is measured on silence as well as failure, and a
    table of failures cannot show a feed that died quietly.
    """
    row = await opsec.record_delivery(
        session,
        user,
        destination_id=payload.destination_id,
        succeeded=payload.succeeded,
        event_count=payload.event_count,
        error=payload.error,
        latency_ms=payload.latency_ms,
    )
    return {"id": str(row.id), "succeeded": row.succeeded}


@router.get("/opsec/siem/health")
async def siem_health(
    session: DbSession,
    user: CurrentUser,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    """Whether each destination is receiving events, including on silence.

    A destination with no errors and no events for longer than its expected
    interval is unhealthy. That is the dangerous state — a SOC with a dead feed
    believes nothing has happened here.
    """
    return await opsec.siem_health(session, user)


@router.get("/opsec/siem/preview")
async def preview_events(
    session: DbSession,
    user: CurrentUser,
    limit: int = 20,
    _hr=Depends(require_roles(*HR_ROLES)),
) -> dict[str, Any]:
    """Recent audit rows mapped onto the wire format.

    Shown rather than described. A SIEM engineer asking "what will arrive" gets
    real events in the real shape.
    """
    return await opsec.preview_events(session, user, limit=limit)


# ── Drills and findings ─────────────────────────────────────────────────────


@router.post("/opsec/drills", status_code=status.HTTP_201_CREATED)
async def record_drill(
    payload: DrillIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> dict[str, Any]:
    """Record a timed restore against the published objectives.

    A drill that took longer than the RTO is stored as a breach, not as a pass
    with a note.
    """
    row = await opsec.record_drill(
        session,
        user,
        tier=payload.tier,
        performed_on=payload.performed_on,
        measured_rto_minutes=payload.measured_rto_minutes,
        measured_rpo_minutes=payload.measured_rpo_minutes,
        succeeded=payload.succeeded,
        notes=payload.notes,
        request=request,
    )
    return {
        "id": str(row.id),
        "tier": row.tier.value,
        "outcome": row.outcome.value,
        "measured_rto_minutes": row.measured_rto_minutes,
        "published_rto_minutes": row.published_rto_minutes,
    }


@router.post("/opsec/findings", status_code=status.HTTP_201_CREATED)
async def record_finding(
    payload: FindingIn,
    session: DbSession,
    user: CurrentUser,
    request: Request,
    _admin=Depends(require_roles(*ADMIN_ROLES)),
) -> dict[str, Any]:
    """Record a pen-test finding, with its SLA clock running from discovery."""
    row = await opsec.record_finding(
        session,
        user,
        reference=payload.reference,
        title=payload.title,
        severity=payload.severity,
        area=payload.area,
        discovered_on=payload.discovered_on,
        tester=payload.tester,
        request=request,
    )
    return {
        "id": str(row.id),
        "reference": row.reference,
        "due_on": row.due_on.isoformat(),
        "sla_days": pentest.REMEDIATION_SLA_DAYS[
            pentest.FindingSeverity(row.severity.value)
        ],
    }


@router.get("/opsec/pentest-scope")
async def pentest_scope(user: CurrentUser) -> dict[str, Any]:
    """The minimum scope a test has to cover for the result to mean anything.

    "We had a pen test" is worth nothing on its own — a test scoped to the
    marketing site is a pen test. Tenant isolation is the highest-value target
    in a multi-tenant HRMS and is in the minimum, along with the agent surface.
    """
    return {
        "required_scope": sorted(a.value for a in pentest.REQUIRED_SCOPE),
        "all_areas": sorted(a.value for a in pentest.ScopeArea),
        "remediation_slas": {
            severity.value: days
            for severity, days in pentest.REMEDIATION_SLA_DAYS.items()
        },
        "sla_statement": (
            "Remediation clocks run from discovery, not from triage. An SLA "
            "measured from triage is met by triaging late."
        ),
    }
