"""The DB-facing half: session records, SIEM destinations, drills and findings.

The continuity numbers, the ECS schema, the session arithmetic and the pen-test
SLAs all live in the pure modules beside this. An invariant checks the isolation.

**What is and is not wired**, stated once here rather than implied:

- Session rows are created, listed and revoked, and for a tenant that has saved
  a policy, `enforcement.py` (called from `get_current_user` on every request)
  applies the idle timeout, the absolute lifetime, per-device revocation and the
  privileged-role IP allowlist.
- Step-up challenges are satisfied by re-entering the account password (checked
  in the router). **No second factor exists** — a password re-check proves the
  person at the keyboard knows it, not that they hold a device.
- SIEM events are mapped and delivery health is computed — **no transport
  exists**, so `record_delivery` is called by a test and by nothing else.

Every one of those is in the posture response as a `not_built` line. A security
questionnaire answered from a half-wired feature is worse than one answered
honestly, because the client verifies it later.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.audit import AuditAction, AuditLog
from app.models.opsec import (
    DataTierEnum,
    DeliveryStateEnum,
    DrillOutcomeEnum,
    FindingSeverityEnum,
    PenTestFinding,
    RestoreDrill,
    SiemDelivery,
    SiemDestination,
    SiemVendorEnum,
    StepUpChallenge,
    TenantSessionPolicy,
    UserSession,
)
from app.models.tenant import Tenant
from app.models.user import User
from app.services import audit
from app.services.opsec import access, continuity, pentest, siem

__all__ = [
    "configure_sessions",
    "list_sessions",
    "posture",
    "record_delivery",
    "record_drill",
    "record_finding",
    "require_step_up",
    "revoke_session",
    "satisfy_challenge",
]


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def _not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


# ══════════════════════════════════════════════════════════════════════════════
# Sessions
# ══════════════════════════════════════════════════════════════════════════════


async def session_policy(
    session: AsyncSession, user: User
) -> access.SessionPolicy:
    stmt = tenant_scoped(select(TenantSessionPolicy), TenantSessionPolicy, user)
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        return access.DEFAULT_SESSION_POLICY
    return access.SessionPolicy(
        idle_timeout_minutes=row.idle_timeout_minutes,
        absolute_lifetime_hours=row.absolute_lifetime_hours,
        max_concurrent_sessions=row.max_concurrent_sessions,
        enforce_ip_allowlist_for_privileged=(
            row.enforce_ip_allowlist_for_privileged
        ),
    )


async def configure_sessions(
    session: AsyncSession,
    user: User,
    *,
    idle_timeout_minutes: int,
    absolute_lifetime_hours: int,
    max_concurrent_sessions: int = 0,
    enforce_ip_allowlist_for_privileged: bool = False,
    ip_allowlist: list[str] | None = None,
    request: Request | None = None,
) -> TenantSessionPolicy:
    """Set the tenant's session rules, validating the pair.

    An idle timeout longer than the absolute lifetime can never fire, and one of
    the two numbers is not what somebody meant — so it is refused rather than
    stored as a setting that does nothing.
    """
    policy = access.SessionPolicy(
        idle_timeout_minutes=idle_timeout_minutes,
        absolute_lifetime_hours=absolute_lifetime_hours,
        max_concurrent_sessions=max_concurrent_sessions,
        enforce_ip_allowlist_for_privileged=enforce_ip_allowlist_for_privileged,
    )
    try:
        policy.validate()
    except access.AccessError as exc:
        raise _bad_request(str(exc)) from exc

    entries = tuple(ip_allowlist or [])
    if entries:
        # Validate every entry now rather than at check time. A malformed CIDR
        # discovered during a login is a lockout; discovered here it is a form
        # error.
        try:
            access.check_allowlist(address="127.0.0.1", entries=entries)
        except access.AccessError as exc:
            raise _bad_request(str(exc)) from exc

    stmt = tenant_scoped(select(TenantSessionPolicy), TenantSessionPolicy, user)
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        row = TenantSessionPolicy(tenant_id=user.tenant_id)
        session.add(row)

    row.idle_timeout_minutes = idle_timeout_minutes
    row.absolute_lifetime_hours = absolute_lifetime_hours
    row.max_concurrent_sessions = max_concurrent_sessions
    row.enforce_ip_allowlist_for_privileged = enforce_ip_allowlist_for_privileged
    row.ip_allowlist = list(entries)
    row.configured_by_user_id = user.id

    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.RULE_CHANGE,
        entity_type="tenant_session_policy",
        entity_id=row.id,
        actor=user,
        summary=(
            f"Session policy set: {idle_timeout_minutes}m idle, "
            f"{absolute_lifetime_hours}h absolute, "
            f"{len(entries)} allowlist entry(ies)."
        ),
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return row


async def start_session(
    session: AsyncSession,
    user: User,
    *,
    token_hash: str,
    ip_address: str | None = None,
    user_agent: str | None = None,
    approximate_location: str | None = None,
    now: datetime | None = None,
) -> UserSession:
    """Record a session, evicting the oldest if the tenant caps concurrency.

    Oldest first. Evicting the newest would let an attacker who has just signed
    in keep their session and push the real user out, which inverts the control.
    """
    moment = now or datetime.now(UTC)
    policy = await session_policy(session, user)

    if policy.max_concurrent_sessions > 0:
        active = (
            (
                await session.execute(
                    tenant_scoped(
                        select(UserSession).where(
                            UserSession.user_id == user.id,
                            UserSession.revoked_at.is_(None),
                        ),
                        UserSession,
                        user,
                    ).order_by(UserSession.started_at)
                )
            )
            .scalars()
            .all()
        )
        evict = access.sessions_to_evict(
            active_started_at=[row.started_at for row in active], policy=policy
        )
        for row in active[:evict]:
            row.revoked_at = moment
            row.revoked_reason = (
                "Displaced by a newer sign-in under this tenant's concurrent "
                "session limit."
            )

    row = UserSession(
        tenant_id=user.tenant_id,
        user_id=user.id,
        token_hash=token_hash,
        started_at=moment,
        last_seen_at=moment,
        ip_address=ip_address,
        user_agent=user_agent,
        approximate_location=approximate_location,
    )
    session.add(row)
    await session.flush()
    await session.commit()
    await session.refresh(row)
    return row


async def list_sessions(
    session: AsyncSession,
    user: User,
    *,
    target_user_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Active sessions with the device and location, so one can be picked out.

    A list of five identical rows is a list nobody acts on, so the address, the
    user agent and the approximate location are all shown — the location with a
    caveat, because a VPN makes it wrong and a UI presenting it as fact teaches
    people to dismiss a real alert.
    """
    moment = now or datetime.now(UTC)
    policy = await session_policy(session, user)
    policy_configured = (
        await session.scalar(
            tenant_scoped(select(TenantSessionPolicy.id), TenantSessionPolicy, user)
        )
    ) is not None
    subject = target_user_id or user.id

    stmt = tenant_scoped(
        select(UserSession).where(UserSession.user_id == subject),
        UserSession,
        user,
    ).order_by(UserSession.last_seen_at.desc())
    rows = (await session.execute(stmt)).scalars().all()

    items = []
    for row in rows:
        state = access.evaluate_session(
            started_at=row.started_at,
            last_seen_at=row.last_seen_at,
            revoked_at=row.revoked_at,
            tokens_valid_from=user.tokens_valid_from if subject == user.id else None,
            policy=policy,
            now=moment,
        )
        items.append(
            {
                "id": str(row.id),
                "started_at": row.started_at.isoformat(),
                "last_seen_at": row.last_seen_at.isoformat(),
                "ip_address": str(row.ip_address) if row.ip_address else None,
                "user_agent": row.user_agent,
                "approximate_location": row.approximate_location,
                "location_caveat": (
                    "Approximate, from the network address. A VPN makes it "
                    "wrong; treat it as a hint rather than a fact."
                ),
                "state": state.value,
                "is_usable": state.is_usable,
                "revoked_at": (
                    row.revoked_at.isoformat() if row.revoked_at else None
                ),
                "revoked_reason": row.revoked_reason,
                "step_up_verified_at": (
                    row.step_up_verified_at.isoformat()
                    if row.step_up_verified_at
                    else None
                ),
            }
        )

    return {
        "policy": policy.as_dict(),
        "sessions": items,
        "active_count": sum(1 for i in items if i["is_usable"]),
        "enforced": policy_configured,
        "not_built": (
            None
            if policy_configured
            else (
                "This workspace has not saved a session policy, so the auth layer "
                "does not consult this policy yet: a session ends when its token "
                "expires or on a forced logout, and per-device tracking is off."
            )
        ),
    }


async def revoke_session(
    session: AsyncSession,
    user: User,
    *,
    session_id: uuid.UUID | None = None,
    all_for_user: uuid.UUID | None = None,
    reason: str = "Signed out by a person.",
    request: Request | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """End one session or every session for a user.

    Revoking all also bumps `tokens_valid_from`, which is what makes it
    immediate: the session rows record the intent, and the token check is what
    actually stops a request that never touches this table.
    """
    moment = now or datetime.now(UTC)

    if session_id is None and all_for_user is None:
        raise _bad_request(
            "Revoking needs either one session or a user to revoke all for."
        )

    stmt = tenant_scoped(select(UserSession), UserSession, user)
    if session_id is not None:
        stmt = stmt.where(UserSession.id == session_id)
    else:
        stmt = stmt.where(UserSession.user_id == all_for_user)
    stmt = stmt.where(UserSession.revoked_at.is_(None))

    rows = (await session.execute(stmt)).scalars().all()
    if session_id is not None and not rows:
        raise _not_found("No such active session.")

    for row in rows:
        row.revoked_at = moment
        row.revoked_reason = reason

    if all_for_user is not None:
        target = await session.get(User, all_for_user)
        if target is None or target.tenant_id != user.tenant_id:
            raise _not_found("No such user.")
        # The half that actually stops a request. A revoked session row does
        # nothing on its own until the middleware reads this table; bumping
        # `tokens_valid_from` invalidates every token issued before now, which
        # the auth layer already checks on every request.
        target.tokens_valid_from = moment

    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.LOGOUT,
        entity_type="user_session",
        entity_id=session_id,
        actor=user,
        summary=(
            f"{len(rows)} session(s) revoked: {reason}"
            + (
                " Tokens issued before now are invalid."
                if all_for_user is not None
                else ""
            )
        ),
        request=request,
    )
    await session.commit()

    return {
        "revoked": len(rows),
        "tokens_invalidated": all_for_user is not None,
        "effective": (
            "Immediate. Revoking all bumps the token validity floor, which the "
            "auth layer checks on every request — it does not wait for each "
            "session to go idle."
            if all_for_user is not None
            else (
                "The session row is marked revoked. Where the workspace has saved "
                "a session policy, the next request from that device is refused; "
                "without one, only revoking all is enforced, because it bumps the "
                "token floor."
            )
        ),
    }


# ══════════════════════════════════════════════════════════════════════════════
# Step-up authentication
# ══════════════════════════════════════════════════════════════════════════════


async def require_step_up(
    session: AsyncSession,
    user: User,
    *,
    action_key: str,
    session_id: uuid.UUID | None = None,
    ip_address: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Whether this action needs fresh proof, and open a challenge if it does.

    Keyed on the action and on how recently the person proved who they are —
    never on session age. An attacker who signed in ten minutes ago with stolen
    credentials has a very fresh session.
    """
    moment = now or datetime.now(UTC)

    if not access.requires_step_up(action_key):
        return {
            "required": False,
            "action_key": action_key,
            "note": (
                "This action does not need step-up. Prompting anyway trains "
                "people to confirm without reading."
            ),
        }

    last_verified: datetime | None = None
    if session_id is not None:
        row = await session.get(UserSession, session_id)
        if row is not None and row.tenant_id == user.tenant_id and row.user_id == user.id:
            last_verified = row.step_up_verified_at

    if access.is_step_up_fresh(last_verified_at=last_verified, now=moment):
        return {
            "required": True,
            "satisfied": True,
            "action_key": action_key,
            "verified_at": last_verified.isoformat() if last_verified else None,
        }

    challenge = StepUpChallenge(
        tenant_id=user.tenant_id,
        user_id=user.id,
        session_id=session_id,
        action_key=action_key,
        reason=access.step_up_reason(action_key),
        ip_address=ip_address,
    )
    session.add(challenge)
    await session.flush()
    await session.commit()
    await session.refresh(challenge)

    return {
        "required": True,
        "satisfied": False,
        "challenge_id": str(challenge.id),
        "action_key": action_key,
        "reason": challenge.reason,
        "window_minutes": int(access.STEP_UP_WINDOW.total_seconds() // 60),
        "not_built": (
            "No second factor is implemented, so nothing can currently satisfy "
            "this challenge through a real verification. `satisfy_challenge` "
            "records a claim; it does not check one."
        ),
    }


async def satisfy_challenge(
    session: AsyncSession,
    user: User,
    *,
    challenge_id: uuid.UUID,
    succeeded: bool,
    request: Request | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Record the outcome of a challenge, successful or not.

    Failures are recorded too. A run of failed challenges against a disbursement
    release is exactly the signal a SOC wants, and it only exists if the
    failures are stored.
    """
    moment = now or datetime.now(UTC)
    stmt = tenant_scoped(
        select(StepUpChallenge).where(StepUpChallenge.id == challenge_id),
        StepUpChallenge,
        user,
    )
    challenge = (await session.execute(stmt)).scalar_one_or_none()
    if challenge is None:
        raise _not_found("No such step-up challenge.")
    if challenge.user_id != user.id:
        # Satisfying somebody else's challenge would lift their step-up.
        raise _not_found("No such step-up challenge.")

    if succeeded:
        challenge.satisfied_at = moment
        if challenge.session_id is not None:
            row = await session.get(UserSession, challenge.session_id)
            if row is not None:
                row.step_up_verified_at = moment
    else:
        challenge.failed_attempts += 1

    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.LOGIN if succeeded else AuditAction.LOGIN_FAILED,
        entity_type="step_up_challenge",
        entity_id=challenge.id,
        actor=user,
        summary=(
            f"Step-up for {challenge.action_key} "
            f"{'satisfied' if succeeded else 'failed'}."
        ),
        request=request,
    )
    await session.commit()

    return {
        "challenge_id": str(challenge.id),
        "satisfied": succeeded,
        "failed_attempts": challenge.failed_attempts,
        "not_built": (
            "No second factor was verified. This records the claim so the "
            "shape of the flow exists; it is not authentication."
        ),
    }


# ══════════════════════════════════════════════════════════════════════════════
# SIEM
# ══════════════════════════════════════════════════════════════════════════════


async def configure_destination(
    session: AsyncSession,
    user: User,
    *,
    name: str,
    vendor: SiemVendorEnum,
    endpoint_url: str,
    expected_interval_minutes: int = 1440,
    datasets: list[str] | None = None,
    request: Request | None = None,
) -> SiemDestination:
    if not endpoint_url.startswith("https://"):
        raise _bad_request(
            "A SIEM endpoint has to be HTTPS. Security events in clear text on "
            "the wire are a second copy of everything the events describe."
        )

    stmt = tenant_scoped(
        select(SiemDestination).where(SiemDestination.name == name),
        SiemDestination,
        user,
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        row = SiemDestination(tenant_id=user.tenant_id, name=name)
        session.add(row)

    row.vendor = vendor
    row.endpoint_url = endpoint_url
    row.expected_interval_minutes = expected_interval_minutes
    row.datasets = list(datasets or [])
    row.active = True

    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.RULE_CHANGE,
        entity_type="siem_destination",
        entity_id=row.id,
        actor=user,
        summary=f"SIEM destination {name!r} configured for {vendor.value}.",
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return row


async def record_delivery(
    session: AsyncSession,
    user: User,
    *,
    destination_id: uuid.UUID,
    succeeded: bool,
    event_count: int = 0,
    error: str | None = None,
    latency_ms: int | None = None,
    now: datetime | None = None,
) -> SiemDelivery:
    """Record one attempt and recompute the destination's health.

    Successes are recorded too, because health is measured on silence: a
    destination with no failures and no events for a day is a dead feed that a
    SOC is trusting, and a table of failures cannot show that.
    """
    moment = now or datetime.now(UTC)
    stmt = tenant_scoped(
        select(SiemDestination).where(SiemDestination.id == destination_id),
        SiemDestination,
        user,
    )
    destination = (await session.execute(stmt)).scalar_one_or_none()
    if destination is None:
        raise _not_found("No such SIEM destination.")

    delivery = SiemDelivery(
        tenant_id=user.tenant_id,
        destination_id=destination.id,
        attempted_at=moment,
        succeeded=succeeded,
        event_count=event_count,
        error=error,
        latency_ms=latency_ms,
    )
    session.add(delivery)

    if succeeded:
        destination.last_success_at = moment
        destination.consecutive_failures = 0
    else:
        destination.last_failure_at = moment
        destination.consecutive_failures += 1

    health = siem.delivery_health(
        destination=destination.name,
        last_success_at=destination.last_success_at,
        last_failure_at=destination.last_failure_at,
        consecutive_failures=destination.consecutive_failures,
        expected_interval_minutes=destination.expected_interval_minutes,
        now=moment,
    )
    destination.health_state = DeliveryStateEnum(health.state.value)

    await session.flush()
    await session.commit()
    await session.refresh(delivery)
    return delivery


async def siem_health(
    session: AsyncSession, user: User, *, now: datetime | None = None
) -> dict[str, Any]:
    """Every destination, its state, and whether somebody should be told.

    Recomputed rather than read from the stored state: a destination that has
    gone quiet has not had a delivery attempt to update its row, so reading the
    column would report it as healthy forever.
    """
    moment = now or datetime.now(UTC)
    stmt = tenant_scoped(select(SiemDestination), SiemDestination, user)
    rows = (await session.execute(stmt)).scalars().all()

    healths = []
    for row in rows:
        health = siem.delivery_health(
            destination=row.name,
            last_success_at=row.last_success_at,
            last_failure_at=row.last_failure_at,
            consecutive_failures=row.consecutive_failures,
            expected_interval_minutes=row.expected_interval_minutes,
            now=moment,
        )
        # Persist the recomputed state so a dashboard reading the column and a
        # caller reading this function agree.
        row.health_state = DeliveryStateEnum(health.state.value)
        healths.append({**health.as_dict(), "vendor": row.vendor.value})

    await session.commit()

    return {
        "destinations": healths,
        "alerting": [h for h in healths if h["needs_alert"]],
        "schema": siem.schema_documentation(),
    }


async def preview_events(
    session: AsyncSession, user: User, *, limit: int = 20
) -> dict[str, Any]:
    """Map recent audit rows onto the wire format, so a client can see it.

    Shown rather than described. A SIEM engineer asking "what will arrive" gets
    real events in the real shape.
    """
    tenant = await session.get(Tenant, user.tenant_id)
    stmt = (
        tenant_scoped(select(AuditLog), AuditLog, user)
        .order_by(AuditLog.created_at.desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).scalars().all()

    events: list[dict[str, Any]] = []
    unmapped: list[str] = []
    for row in rows:
        try:
            event = siem.to_ecs(
                audit_action=row.action.value,
                timestamp=row.created_at,
                event_id=str(row.id),
                tenant_id=str(row.tenant_id),
                tenant_name=tenant.name if tenant else "",
                summary=row.summary or row.action.value,
                user_id=str(row.actor_user_id) if row.actor_user_id else None,
                source_ip=str(row.ip_address) if row.ip_address else None,
                user_agent=row.user_agent,
                entity_type=row.entity_type,
                entity_id=str(row.entity_id) if row.entity_id else None,
                actor_type=row.actor_type,
            )
        except siem.SiemError:
            unmapped.append(row.action.value)
            continue
        events.append(event.as_wire())

    return {
        "events": events,
        "unmapped_actions": sorted(set(unmapped)),
        "schema": siem.schema_documentation(),
    }


# ══════════════════════════════════════════════════════════════════════════════
# Drills and findings
# ══════════════════════════════════════════════════════════════════════════════


async def record_drill(
    session: AsyncSession,
    user: User,
    *,
    tier: DataTierEnum,
    performed_on: date,
    measured_rto_minutes: int,
    measured_rpo_minutes: int,
    succeeded: bool,
    notes: str = "",
    request: Request | None = None,
) -> RestoreDrill:
    """Record a timed restore, judged against the published objectives.

    A drill that took longer than the RTO is stored as a breach, not as a pass
    with a note. Rounding it into a pass is how a published number survives the
    evidence against it.
    """
    try:
        result = continuity.evaluate_drill(
            tier=continuity.DataTier(tier.value),
            performed_on=performed_on,
            measured_rto_minutes=measured_rto_minutes,
            measured_rpo_minutes=measured_rpo_minutes,
            succeeded=succeeded,
            notes=notes,
        )
    except continuity.ContinuityError as exc:
        raise _bad_request(str(exc)) from exc

    target = continuity.TIERS[continuity.DataTier(tier.value)]
    row = RestoreDrill(
        tenant_id=user.tenant_id,
        tier=tier,
        performed_on=performed_on,
        measured_rto_minutes=measured_rto_minutes,
        measured_rpo_minutes=measured_rpo_minutes,
        # The targets as published on the day, so revising a number later does
        # not retroactively turn a breach into a pass.
        published_rto_minutes=target.rto_minutes,
        published_rpo_minutes=target.rpo_minutes,
        succeeded=succeeded,
        outcome=DrillOutcomeEnum(result.outcome.value),
        notes=notes or None,
        performed_by_user_id=user.id,
    )
    session.add(row)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="restore_drill",
        entity_id=row.id,
        actor=user,
        summary=(
            f"Restore drill on the {tier.value} tier: "
            f"{measured_rto_minutes}m against a published {target.rto_minutes}m "
            f"— {result.outcome.value}."
        ),
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return row


async def record_finding(
    session: AsyncSession,
    user: User,
    *,
    reference: str,
    title: str,
    severity: FindingSeverityEnum,
    area: str,
    discovered_on: date,
    tester: str | None = None,
    request: Request | None = None,
) -> PenTestFinding:
    """Record a finding, with its SLA clock running from discovery."""
    try:
        scope_area = pentest.ScopeArea(area)
    except ValueError as exc:
        raise _bad_request(
            f"{area!r} is not a scope area. Known: "
            f"{', '.join(a.value for a in pentest.ScopeArea)}."
        ) from exc

    finding = pentest.Finding(
        reference=reference,
        title=title,
        severity=pentest.FindingSeverity(severity.value),
        area=scope_area,
        discovered_on=discovered_on,
    )

    row = PenTestFinding(
        tenant_id=user.tenant_id,
        reference=reference,
        title=title,
        severity=severity,
        area=area,
        discovered_on=discovered_on,
        due_on=finding.due_on,
        tester=tester,
    )
    session.add(row)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="pentest_finding",
        entity_id=row.id,
        actor=user,
        summary=(
            f"{severity.value} finding {reference}: {title[:120]}. Due "
            f"{finding.due_on.isoformat()}."
        ),
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return row


async def registers(session: AsyncSession, user: User) -> dict[str, Any]:
    """The drill and pen-test registers and the SIEM destinations, as rows."""
    drills = (
        await session.scalars(
            tenant_scoped(select(RestoreDrill), RestoreDrill, user).order_by(
                RestoreDrill.performed_on.desc()
            )
        )
    ).all()
    findings = (
        await session.scalars(
            tenant_scoped(select(PenTestFinding), PenTestFinding, user).order_by(
                PenTestFinding.discovered_on.desc()
            )
        )
    ).all()
    destinations = (
        await session.scalars(
            tenant_scoped(select(SiemDestination), SiemDestination, user).order_by(
                SiemDestination.name
            )
        )
    ).all()
    return {
        "drills": [
            {
                "id": str(d.id),
                "tier": d.tier.value,
                "performed_on": d.performed_on.isoformat(),
                "measured_rto_minutes": d.measured_rto_minutes,
                "measured_rpo_minutes": d.measured_rpo_minutes,
                "published_rto_minutes": d.published_rto_minutes,
                "published_rpo_minutes": d.published_rpo_minutes,
                "succeeded": d.succeeded,
                "outcome": d.outcome.value,
                "notes": d.notes,
            }
            for d in drills
        ],
        "findings": [
            {
                "id": str(f.id),
                "reference": f.reference,
                "title": f.title,
                "severity": f.severity.value,
                "area": f.area,
                "discovered_on": f.discovered_on.isoformat(),
                "due_on": f.due_on.isoformat(),
                "remediated_on": f.remediated_on.isoformat() if f.remediated_on else None,
                "risk_accepted_reason": f.risk_accepted_reason,
                "tester": f.tester,
            }
            for f in findings
        ],
        "siem_destinations": [
            {
                "id": str(d.id),
                "name": d.name,
                "vendor": d.vendor.value,
                "endpoint_url": d.endpoint_url,
                "active": d.active,
                "expected_interval_minutes": d.expected_interval_minutes,
                "datasets": d.datasets,
                "health_state": d.health_state.value,
                "last_success_at": (
                    d.last_success_at.isoformat() if d.last_success_at else None
                ),
                "last_failure_at": (
                    d.last_failure_at.isoformat() if d.last_failure_at else None
                ),
            }
            for d in destinations
        ],
    }


async def policy_settings(session: AsyncSession, user: User) -> dict[str, Any]:
    """The saved session policy, or the defaults marked as not saved."""
    row = await session.scalar(
        tenant_scoped(select(TenantSessionPolicy), TenantSessionPolicy, user)
    )
    if row is None:
        defaults = access.DEFAULT_SESSION_POLICY
        return {
            "configured": False,
            "idle_timeout_minutes": defaults.idle_timeout_minutes,
            "absolute_lifetime_hours": defaults.absolute_lifetime_hours,
            "max_concurrent_sessions": defaults.max_concurrent_sessions,
            "enforce_ip_allowlist_for_privileged": False,
            "ip_allowlist": [],
        }
    return {
        "configured": True,
        "idle_timeout_minutes": row.idle_timeout_minutes,
        "absolute_lifetime_hours": row.absolute_lifetime_hours,
        "max_concurrent_sessions": row.max_concurrent_sessions,
        "enforce_ip_allowlist_for_privileged": row.enforce_ip_allowlist_for_privileged,
        "ip_allowlist": list(row.ip_allowlist or []),
    }


async def active_sessions_everyone(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    """Every tracked, unrevoked session in the workspace, newest activity first."""
    rows = (
        await session.execute(
            tenant_scoped(
                select(UserSession, User)
                .join(User, User.id == UserSession.user_id)
                .where(UserSession.revoked_at.is_(None)),
                UserSession,
                user,
            )
            .order_by(UserSession.last_seen_at.desc())
            .limit(500)
        )
    ).all()
    return [
        {
            "id": str(s.id),
            "user_id": str(u.id),
            "user_name": u.full_name,
            "user_email": u.email,
            "role": u.role.value,
            "started_at": s.started_at.isoformat(),
            "last_seen_at": s.last_seen_at.isoformat(),
            "ip_address": str(s.ip_address) if s.ip_address else None,
            "user_agent": s.user_agent,
        }
        for s, u in rows
    ]


async def posture(
    session: AsyncSession, user: User, *, today: date | None = None
) -> dict[str, Any]:
    """The whole operational-security posture, as a procurement questionnaire asks.

    Every published number is shown with whether it is currently evidenced, and
    everything unbuilt is listed. A questionnaire answered from a half-wired
    feature is worse than one answered honestly, because the client verifies it
    later — usually during an incident.
    """
    as_of = today or datetime.now(UTC).date()

    drill_rows = (
        (
            await session.execute(
                tenant_scoped(select(RestoreDrill), RestoreDrill, user)
            )
        )
        .scalars()
        .all()
    )
    drills = [
        continuity.DrillResult(
            tier=continuity.DataTier(row.tier.value),
            performed_on=row.performed_on,
            measured_rto_minutes=row.measured_rto_minutes,
            measured_rpo_minutes=row.measured_rpo_minutes,
            succeeded=row.succeeded,
            outcome=continuity.DrillOutcome(row.outcome.value),
            notes=row.notes or "",
        )
        for row in drill_rows
    ]

    finding_rows = (
        (
            await session.execute(
                tenant_scoped(select(PenTestFinding), PenTestFinding, user)
            )
        )
        .scalars()
        .all()
    )
    findings = [
        pentest.Finding(
            reference=row.reference,
            title=row.title,
            severity=pentest.FindingSeverity(row.severity.value),
            area=pentest.ScopeArea(row.area),
            discovered_on=row.discovered_on,
            remediated_on=row.remediated_on,
            risk_accepted_reason=row.risk_accepted_reason or "",
        )
        for row in finding_rows
    ]
    covered = frozenset(f.area for f in findings)

    destinations = (
        await session.execute(
            tenant_scoped(
                select(func.count()).select_from(SiemDestination),
                SiemDestination,
                user,
            )
        )
    ).scalar_one()

    return {
        "continuity": continuity.published_posture(drills, today=as_of),
        "access": access.posture_summary(),
        "penetration_testing": pentest.summary_report(
            tested_on=min((f.discovered_on for f in findings), default=as_of),
            tester=next((r.tester for r in finding_rows if r.tester), "not tested"),
            covered=covered,
            findings=findings,
            today=as_of,
        ),
        "siem": {
            "destinations_configured": destinations,
            **siem.schema_documentation(),
        },
        "not_built": [
            "No backup, restore or failover machinery is in this codebase — "
            "that is infrastructure. This module holds the published objectives "
            "and the record of drills against them.",
            "No third-party penetration test has been performed.",
            "No SIEM transport exists; events are mapped and never shipped.",
            "The session policy and IP allowlist are enforced by the auth "
            "middleware only for a workspace that has saved a policy; the "
            "allowlist reads the socket address, never X-Forwarded-For, so a "
            "proxy must pass the client address to the server.",
            "No second factor exists: step-up re-checks the account password "
            "at the step-up endpoint, and no device factor is built.",
            "SIEM delivery-failure alerting is computed and shown, and no alert "
            "is sent because no transport exists.",
            "Dependency and container scanning are CI configuration and are not "
            "present in this repository.",
        ],
    }
