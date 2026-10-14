"""The DB-facing half: configuration, the queue, reversal and the signal.

The ceiling, the routing, the expiry and the rates all live in the pure modules
beside this. An invariant checks the isolation, and a second one checks the
protected actions directly by key — so the ceiling cannot be lifted by editing a
config row, a request body, this file, or the catalogue's `max_autonomy` field.

`propose` is the one entry point an agent uses. The agents that call it live
in `agents.py`; today that is the probation agent, which proposes the
confirmations a tenant's auto-confirm probation policy calls for.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.autonomy import (
    AgentAutonomyConfig,
    AgentProposal,
    AutonomyLevelEnum,
    ProposalReversal,
    ProposalStatusEnum,
)
from app.models.user import User
from app.services import audit
from app.services.autonomy import proposals as proposal_engine
from app.services.autonomy import subjects as subject_registry
from app.services.autonomy.catalogue import (
    ACTIONS,
    AutonomyError,
    AutonomyLevel,
    action,
    ceiling_for,
    protected_actions,
    validate_autonomy,
)

__all__ = [
    "configure",
    "decide_proposal",
    "expire_stale",
    "propose",
    "queue",
    "revert",
    "transparency_surface",
]


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def _not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


# ══════════════════════════════════════════════════════════════════════════════
# Configuration
# ══════════════════════════════════════════════════════════════════════════════


async def configure(
    session: AsyncSession,
    user: User,
    *,
    action_key: str,
    autonomy: AutonomyLevel,
    threshold_value: Decimal | None = None,
    approver_roles: list[str] | None = None,
    request: Request | None = None,
    now: datetime | None = None,
) -> AgentAutonomyConfig:
    """Set one action's autonomy, refusing anything above the ceiling.

    The refusal names the action, the ceiling and the reason. A client asking
    for auto-apply on payroll rule changes is told no and told why — a message
    saying "invalid configuration" means they ask again next quarter.
    """
    try:
        validated = validate_autonomy(
            action_key, autonomy, threshold=threshold_value
        )
    except AutonomyError as exc:
        raise _bad_request(str(exc)) from exc

    spec = action(action_key)
    stmt = tenant_scoped(
        select(AgentAutonomyConfig).where(
            AgentAutonomyConfig.action_key == action_key
        ),
        AgentAutonomyConfig,
        user,
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        row = AgentAutonomyConfig(
            tenant_id=user.tenant_id,
            action_key=action_key,
            agent_key=spec.agent_key,
        )
        session.add(row)

    row.autonomy = AutonomyLevelEnum(validated.value)
    # The ceiling as it stood now. Stored so lowering one later does not rewrite
    # the history of what this tenant was allowed to choose.
    row.max_autonomy_at_time = AutonomyLevelEnum(ceiling_for(action_key).value)
    row.approver_roles = list(approver_roles or [])
    row.threshold_value = threshold_value
    row.threshold_unit = spec.threshold_unit
    row.configured_by_user_id = user.id
    row.configured_at = now or datetime.now(UTC)

    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.RULE_CHANGE,
        entity_type="agent_autonomy_config",
        entity_id=row.id,
        actor=user,
        summary=(
            f"{action_key} autonomy set to {validated.value}"
            + (f" below {threshold_value} {spec.threshold_unit}" if threshold_value else "")
            + "."
        ),
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return row


async def effective_autonomy(
    session: AsyncSession, user: User, *, action_key: str
) -> tuple[AutonomyLevel, Decimal | None, str]:
    """What this tenant's setting is, capped at today's ceiling.

    Capped on read as well as on write. The write-time check is the one that
    gives a client a useful error; this one is what makes a stored value from
    before a ceiling was lowered safe.
    """
    spec = action(action_key)
    stmt = tenant_scoped(
        select(AgentAutonomyConfig).where(
            AgentAutonomyConfig.action_key == action_key
        ),
        AgentAutonomyConfig,
        user,
    )
    row = (await session.execute(stmt)).scalar_one_or_none()

    configured = (
        AutonomyLevel(row.autonomy.value) if row else spec.default_autonomy
    )
    ceiling = ceiling_for(action_key)
    if configured.rank > ceiling.rank:
        return (
            ceiling,
            Decimal(str(row.threshold_value)) if row and row.threshold_value else None,
            (
                f"The stored setting {configured.value!r} is above today's "
                f"ceiling of {ceiling.value!r} and has been capped. The ceiling "
                "was lowered after this was configured."
            ),
        )
    threshold = (
        Decimal(str(row.threshold_value)) if row and row.threshold_value else None
    )
    source = "configured by this tenant" if row else "platform default"
    return configured, threshold, source


async def list_configuration(session: AsyncSession, user: User) -> dict[str, Any]:
    """Every action, its ceiling, and what this tenant has chosen.

    Includes the actions the tenant has *not* configured, showing the default —
    a page listing only overrides makes an unconfigured action look like an
    absent one.
    """
    stmt = tenant_scoped(select(AgentAutonomyConfig), AgentAutonomyConfig, user)
    rows = {row.action_key: row for row in (await session.execute(stmt)).scalars()}

    out = []
    for key, spec in ACTIONS.items():
        row = rows.get(key)
        out.append(
            {
                **spec.as_dict(),
                "configured_autonomy": (
                    row.autonomy.value if row else spec.default_autonomy.value
                ),
                "is_tenant_configured": row is not None,
                "threshold_value": (
                    float(row.threshold_value)
                    if row and row.threshold_value is not None
                    else None
                ),
                "approver_roles": row.approver_roles if row else [],
                "configured_at": (
                    row.configured_at.isoformat() if row else None
                ),
            }
        )
    return {
        "actions": out,
        "protected": [a.as_dict() for a in protected_actions()],
        "protected_statement": (
            "These four can never be applied automatically, whatever the "
            "configuration says. The ceiling is enforced when a setting is "
            "written and again when it is read, and a test asserts each one "
            "directly by name."
        ),
    }


# ══════════════════════════════════════════════════════════════════════════════
# The queue
# ══════════════════════════════════════════════════════════════════════════════


async def propose(
    session: AsyncSession,
    user: User,
    *,
    action_key: str,
    subject_type: str,
    subject_id: uuid.UUID,
    proposed_change: dict[str, Any],
    previous_state: dict[str, Any],
    reasoning: str,
    citations: tuple[str, ...],
    confidence: float,
    magnitude: Decimal | None = None,
    request: Request | None = None,
    now: datetime | None = None,
) -> AgentProposal:
    """Create a proposal, routing it by the tenant's configuration.

    Refuses one an approver could not evaluate — no reasoning, no citations, or
    a reversible action with no captured previous state. A proposal that cannot
    be judged gets approved in a batch, and the approval step becomes latency
    with no safety.
    """
    moment = now or datetime.now(UTC)
    spec = action(action_key)

    engine_proposal = proposal_engine.Proposal(
        action_key=action_key,
        subject_type=subject_type,
        subject_id=str(subject_id),
        proposed_change=proposed_change,
        reasoning=reasoning,
        citations=citations,
        confidence=confidence,
        created_at=moment,
        previous_state=previous_state,
    )
    try:
        proposal_engine.assert_evaluable(engine_proposal)
    except AutonomyError as exc:
        raise _bad_request(str(exc)) from exc

    configured, threshold, _source = await effective_autonomy(
        session, user, action_key=action_key
    )
    routing = proposal_engine.route(
        action_key,
        configured=configured,
        magnitude=magnitude,
        threshold=threshold,
    )

    if routing.autonomy is AutonomyLevel.SUGGEST_ONLY:
        raise _bad_request(
            f"{spec.description} is set to {AutonomyLevel.SUGGEST_ONLY.label!r} "
            "for this tenant, so it does not create proposals. A suggestion is "
            "shown and nothing is queued."
        )

    row = AgentProposal(
        tenant_id=user.tenant_id,
        action_key=action_key,
        agent_key=spec.agent_key,
        subject_type=subject_type,
        subject_id=subject_id,
        proposed_change=proposed_change,
        previous_state=previous_state,
        reasoning=reasoning,
        citations=list(citations),
        confidence=confidence,
        impact=spec.impact.value,
        reversible=spec.reversible,
        status=(
            ProposalStatusEnum.AUTO_APPLIED
            if routing.applies_automatically
            else ProposalStatusEnum.PENDING
        ),
        # Stamped from the same clock as `expires_at`. Left to the column
        # default, age (now - created_at) and expiry were measured from two
        # different clocks whenever `now` was supplied, and a proposal could be
        # "negative days old" at the moment it expired.
        created_at=moment,
        expires_at=proposal_engine.expires_at(action_key, created_at=moment),
        routing_reason=routing.reason,
        applied_at=moment if routing.applies_automatically else None,
    )
    if routing.applies_automatically:
        captured = await subject_registry.apply_change(
            session,
            user,
            subject_type=subject_type,
            subject_id=subject_id,
            change=dict(proposed_change),
        )
        if captured is not None:
            row.previous_state = captured
    session.add(row)
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.AI_ACTION,
        entity_type="agent_proposal",
        entity_id=row.id,
        actor=user,
        actor_type="ai_companion",
        summary=(
            f"{spec.description} "
            + (
                "applied automatically"
                if routing.applies_automatically
                else "proposed for approval"
            )
            + f" for {subject_type} {subject_id}. {routing.reason}"
        ),
        ai_context={
            "action_key": action_key,
            "confidence": confidence,
            "citations": list(citations),
            "autonomy": routing.autonomy.value,
        },
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return row


async def queue(
    session: AsyncSession,
    user: User,
    *,
    agent_key: str | None = None,
    impact: str | None = None,
    min_age_days: int | None = None,
    include_decided: bool = False,
    limit: int = 100,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The one consolidated queue, filterable by agent, impact and age.

    One list for every agent. An HR Ops lead with five per-agent inboxes checks
    two of them, and the other three become a backlog nobody owns.
    """
    moment = now or datetime.now(UTC)
    stmt = tenant_scoped(select(AgentProposal), AgentProposal, user)
    if not include_decided:
        stmt = stmt.where(AgentProposal.status == ProposalStatusEnum.PENDING)
    if agent_key:
        stmt = stmt.where(AgentProposal.agent_key == agent_key)
    if impact:
        stmt = stmt.where(AgentProposal.impact == impact)

    rows = (
        (
            await session.execute(
                stmt.order_by(AgentProposal.expires_at).limit(limit)
            )
        )
        .scalars()
        .all()
    )

    items = []
    for row in rows:
        age = (moment - row.created_at).days
        if min_age_days is not None and age < min_age_days:
            continue
        items.append(
            {
                "id": str(row.id),
                "action_key": row.action_key,
                "agent_key": row.agent_key,
                "description": (
                    ACTIONS[row.action_key].description
                    if row.action_key in ACTIONS
                    else row.action_key
                ),
                "subject_type": row.subject_type,
                "subject_id": str(row.subject_id),
                "proposed_change": row.proposed_change,
                # Both present on every row. An approver who cannot see why will
                # approve forty in a batch.
                "reasoning": row.reasoning,
                "citations": row.citations,
                "confidence": float(row.confidence),
                "impact": row.impact,
                "reversible": row.reversible,
                "status": row.status.value,
                "routing_reason": row.routing_reason,
                "created_at": row.created_at.isoformat(),
                "expires_at": row.expires_at.isoformat(),
                "age_days": age,
                "is_expired": moment > row.expires_at,
            }
        )

    return {
        "proposals": items,
        "agents_represented": sorted({i["agent_key"] for i in items}),
        "note": (
            "One queue for every agent. A per-agent inbox produces a person "
            "with five tabs who checks two of them."
        ),
    }


async def decide_proposal(
    session: AsyncSession,
    user: User,
    *,
    proposal_id: uuid.UUID,
    approve: bool,
    note: str = "",
    request: Request | None = None,
    now: datetime | None = None,
) -> AgentProposal:
    """Approve or reject. An expired proposal cannot be approved.

    A statutory rate proposed in March and approved in September applies a rate
    superseded in June to a payroll that has already run. The proposal was right
    when it was made; nothing on the approval screen would say it is wrong now.
    """
    moment = now or datetime.now(UTC)
    row = await _proposal_or_404(session, user, proposal_id)

    # The **stored** expiry gates the decision, not one recomputed from
    # `created_at` and today's window. That is the whole reason `expires_at` is
    # a column: recomputing it means lengthening a window next month revives
    # proposals that had already lapsed, and it also means a proposal whose
    # creation timestamp came from the server clock while its expiry came from
    # a supplied moment can never expire at all — which is how this was found.
    if row.status is ProposalStatusEnum.PENDING and moment > row.expires_at:
        raise _bad_request(
            f"This proposal expired on {row.expires_at.date().isoformat()} and "
            "cannot be applied. It was made against facts that may since have "
            "changed — if it is still wanted, propose it again against today's "
            "data."
        )

    engine_proposal = proposal_engine.Proposal(
        action_key=row.action_key,
        subject_type=row.subject_type,
        subject_id=str(row.subject_id),
        proposed_change=row.proposed_change,
        reasoning=row.reasoning,
        citations=tuple(row.citations),
        confidence=float(row.confidence),
        created_at=row.created_at,
        status=proposal_engine.ProposalStatus(row.status.value),
        previous_state=row.previous_state,
    )
    try:
        decided = proposal_engine.decide(
            engine_proposal, approve=approve, now=moment, note=note
        )
    except AutonomyError as exc:
        raise _bad_request(str(exc)) from exc

    row.status = ProposalStatusEnum(decided.status.value)
    row.decided_by_user_id = user.id
    row.decided_at = moment
    row.decision_note = note or None
    if approve:
        captured = await subject_registry.apply_change(
            session,
            user,
            subject_type=row.subject_type,
            subject_id=row.subject_id,
            change=dict(row.proposed_change or {}),
        )
        if captured is not None:
            # What the record actually held, read at the moment of applying — the
            # state a revert restores. A previous state supplied with the proposal
            # can be stale by the time somebody approves it.
            row.previous_state = captured
        row.applied_at = moment

    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.APPROVE if approve else AuditAction.REJECT,
        entity_type="agent_proposal",
        entity_id=row.id,
        actor=user,
        summary=(
            f"{row.action_key} proposal "
            f"{'approved' if approve else 'rejected'} for {row.subject_type} "
            f"{row.subject_id}." + (f" {note}" if note else "")
        ),
        request=request,
    )
    await session.commit()
    await session.refresh(row)
    return row


async def _proposal_or_404(
    session: AsyncSession, user: User, proposal_id: uuid.UUID
) -> AgentProposal:
    stmt = tenant_scoped(
        select(AgentProposal).where(AgentProposal.id == proposal_id),
        AgentProposal,
        user,
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise _not_found("No such proposal.")
    return row


async def expire_stale(
    session: AsyncSession,
    user: User,
    *,
    now: datetime | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    """Mark lapsed proposals expired. Never applies one late.

    The expired ones are reported rather than swept away: a queue producing
    expiries is a queue nobody is working, and that is the problem worth seeing.
    """
    moment = now or datetime.now(UTC)
    stmt = tenant_scoped(
        select(AgentProposal).where(
            AgentProposal.status == ProposalStatusEnum.PENDING,
            AgentProposal.expires_at < moment,
        ),
        AgentProposal,
        user,
    )
    rows = (await session.execute(stmt)).scalars().all()
    for row in rows:
        row.status = ProposalStatusEnum.EXPIRED

    if rows:
        await session.flush()
        await audit.record(
            session,
            tenant_id=user.tenant_id,
            action=AuditAction.UPDATE,
            entity_type="agent_proposal",
            actor=user,
            summary=(
                f"{len(rows)} proposal(s) expired unread. None was applied."
            ),
            request=request,
        )
    await session.commit()

    return {
        "expired": len(rows),
        "by_action": _count_by(row.action_key for row in rows),
        "note": (
            "Expired proposals were never applied. A queue producing these is "
            "one nobody is working, which is a different problem from an agent "
            "producing bad proposals — and the rejection-rate metric keeps the "
            "two apart."
        ),
    }


# ══════════════════════════════════════════════════════════════════════════════
# Reversal
# ══════════════════════════════════════════════════════════════════════════════


async def revert(
    session: AsyncSession,
    user: User,
    *,
    proposal_id: uuid.UUID,
    reason: str,
    request: Request | None = None,
    now: datetime | None = None,
    outcome: dict[str, Any] | None = None,
) -> ProposalReversal:
    """Undo an applied action, restoring the state captured at proposal time.

    For a subject type registered in `subjects.py` the captured values are
    written back to the record in the same transaction; `outcome`, when given,
    is filled with `restored_to_record`.

    The reversal row is always written. The record itself is restored only for
    registered subject types; for any other the response says it was not touched.
    """
    moment = now or datetime.now(UTC)
    row = await _proposal_or_404(session, user, proposal_id)

    if not reason.strip():
        raise _bad_request(
            "A reversal needs a reason. It is the record of why an applied "
            "action was undone, and somebody will ask."
        )
    if not row.reversible:
        raise _bad_request(
            f"{row.action_key} is not a reversible action. It could never have "
            "been applied automatically, and an approved one cannot be undone "
            "by this route — the change has to be made deliberately, by a "
            "person, as a new decision."
        )
    # Checked before the "never applied" branch. A reverted proposal *was*
    # applied, and telling somebody it was not is a worse answer than telling
    # them it is already undone.
    if row.status is ProposalStatusEnum.REVERTED:
        raise _bad_request("That proposal has already been reverted.")

    if row.status not in (
        ProposalStatusEnum.APPROVED,
        ProposalStatusEnum.AUTO_APPLIED,
    ):
        raise _bad_request(
            f"That proposal is {row.status.value} and was never applied, so "
            "there is nothing to revert."
        )

    existing = (
        await session.execute(
            select(ProposalReversal).where(
                ProposalReversal.proposal_id == proposal_id
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise _bad_request("That proposal has already been reverted.")

    reversal = ProposalReversal(
        tenant_id=user.tenant_id,
        proposal_id=row.id,
        # Copied, not referenced. A reversal that has to read the proposal to
        # say what it restored breaks if retention ever purges the proposal.
        restored_state=dict(row.previous_state or {}),
        reason=reason.strip(),
        reverted_at=moment,
        reverted_by_user_id=user.id,
    )
    session.add(reversal)
    row.status = ProposalStatusEnum.REVERTED
    restored = False
    if row.applied_at is not None:
        restored = await subject_registry.restore(
            session,
            user,
            subject_type=row.subject_type,
            subject_id=row.subject_id,
            previous=dict(row.previous_state or {}),
        )
    if outcome is not None:
        outcome["restored_to_record"] = restored

    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="agent_proposal",
        entity_id=row.id,
        actor=user,
        summary=(
            f"{row.action_key} reverted for {row.subject_type} "
            f"{row.subject_id}: {reason.strip()[:160]}"
        ),
        request=request,
    )
    await session.commit()
    await session.refresh(reversal)
    return reversal


# ══════════════════════════════════════════════════════════════════════════════
# The signal, and the client-facing surface
# ══════════════════════════════════════════════════════════════════════════════


def _count_by(values: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        out[value] = out.get(value, 0) + 1
    return out


async def decision_stats(session: AsyncSession, user: User) -> dict[str, Any]:
    """Approval and rejection rates per action, and what looks defective.

    This is a **product signal to us**, not only a client metric. A proposal
    type rejected ninety per cent of the time is a defective agent, and the
    client experiencing it will not file a bug — they will turn the agent off
    and mention it at renewal.
    """
    stmt = tenant_scoped(
        select(AgentProposal.action_key, AgentProposal.status, func.count())
        .group_by(AgentProposal.action_key, AgentProposal.status),
        AgentProposal,
        user,
    )
    rows = (await session.execute(stmt)).all()

    grouped: dict[str, list[proposal_engine.ProposalStatus]] = {}
    for action_key, proposal_status, count in rows:
        grouped.setdefault(action_key, []).extend(
            [proposal_engine.ProposalStatus(proposal_status.value)] * count
        )

    stats = [
        proposal_engine.stats_for(key, statuses).as_dict()
        for key, statuses in sorted(grouped.items())
        if key in ACTIONS
    ]
    return {
        "actions": stats,
        "looks_defective": [s["action_key"] for s in stats if s["looks_defective"]],
        "note": (
            "A high rejection rate is a signal about the agent, not about the "
            "client. Expiries are counted separately — an unworked queue and a "
            "bad agent need opposite responses."
        ),
    }


async def transparency_surface(
    session: AsyncSession, user: User, *, now: datetime | None = None
) -> dict[str, Any]:
    """What each agent is permitted to do, has done, is waiting on, and reverted.

    The concrete answer to "what is your AI actually doing in my HR system",
    which is asked in every enterprise security review. Built from the same
    records the queue reads, so it cannot say something the queue would
    contradict.
    """
    moment = now or datetime.now(UTC)
    config = await list_configuration(session, user)

    counts_stmt = tenant_scoped(
        select(AgentProposal.status, func.count()).group_by(AgentProposal.status),
        AgentProposal,
        user,
    )
    counts = {
        row_status.value: count
        for row_status, count in (await session.execute(counts_stmt)).all()
    }

    reversals_stmt = tenant_scoped(
        select(func.count()).select_from(ProposalReversal), ProposalReversal, user
    )
    reversal_count = (await session.execute(reversals_stmt)).scalar_one()

    return {
        "permitted": config["actions"],
        "protected": config["protected"],
        "protected_statement": config["protected_statement"],
        "activity": {
            "pending": counts.get("pending", 0),
            "approved": counts.get("approved", 0),
            "rejected": counts.get("rejected", 0),
            "auto_applied": counts.get("auto_applied", 0),
            "expired": counts.get("expired", 0),
            "reverted": counts.get("reverted", 0),
        },
        "reversals_recorded": reversal_count,
        # The queue carries each proposal's subject, change and reasoning -
        # somebody's salary, somebody's rating. Counts are for everybody; the
        # queue and the decision record are for the people who decide.
        "pending_queue": (
            (await queue(session, user, now=moment))["proposals"]
            if permissions.is_hr(user)
            else []
        ),
        "decision_stats": (
            await decision_stats(session, user) if permissions.is_hr(user) else None
        ),
        "not_built": [
            "One agent creates proposals: the probation agent "
            "(lifecycle.raise_confirmation), daily, under an auto-confirm probation "
            "policy. The other actions in the catalogue have no agent yet, so any "
            "proposal for them came from a manual call.",
            "Approving applies the change, and reverting restores it, only for "
            "regularisations, expense claims, candidates and lifecycle events; any "
            "other subject type is recorded as a decision and its record is not "
            "touched.",
            "Rejected proposals are not yet fed into the slice 27 eval set; the "
            "column linking them exists and nothing writes it.",
        ],
    }
