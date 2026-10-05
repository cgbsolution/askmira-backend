"""Everything waiting on the caller's decision, across modules. Read-only.

Nothing is decided here. Each item links to the page in its own module where
it is decided, and that module's endpoint is still the authority on whether
this person may decide it. What this module has to get right is the other
direction: **never show somebody an item they could not act on**, and never
show them an item about themselves.

So each source mirrors its module's own rule:

- the role gate on the module's decide endpoint (an HR-ops user is not shown a
  loan, because approving a loan is admin-only);
- the module's visibility rule through `app.core.permissions` (a manager sees
  regularisations from their reporting line and nobody else's);
- the workspace's enabled modules (no payroll items where payroll is off);
- and self-exclusion, because nobody decides their own request.

A source that refuses (a login with no employee record asking for leave
approvals, say) is reported as unavailable rather than failing the whole inbox.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from fastapi import HTTPException
from sqlalchemy import Select, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.attendance import AttendanceRegularisation, RegularisationStatus
from app.models.autonomy import AgentProposal, ProposalStatusEnum
from app.models.dpdp import DataPrincipalRequest, DprRequestStatus
from app.models.employee import Employee
from app.models.exit_workflow import Settlement, SettlementStatus
from app.models.expenses import (
    ExpenseClaim,
    ExpenseClaimStatusEnum,
    TravelRequest,
    TravelRequestStatusEnum,
)
from app.models.hiring import Requisition, RequisitionStatus
from app.models.lifecycle import LifecycleEvent, LifecycleStatus
from app.models.loans import LoanAccount, LoanStatus
from app.models.offer import (
    ApprovalDecision,
    Offer,
    OfferApproval,
    OfferStatus,
    RequisitionApproval,
)
from app.models.retention import RetentionRun, RetentionRunStatus
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.schemas.approvals import ApprovalInboxRead, ApprovalItemRead, ApprovalSourceRead
from app.services import employee_profile as profile_service
from app.services.assets import service as assets_service
from app.services.documents import vault as documents_vault
from app.services.exit_workflow import clearance_tasks
from app.services.exit_workflow import resignation as resignation_service
from app.services.expenses import inbox as expense_inbox
from app.services.leave import comp_off as comp_off_service
from app.services.leave import service as leave_service
from app.services.payroll import inbox as payroll_inbox
from app.services.performance import feedback as feedback_service

__all__ = ["SOURCES", "inbox"]

logger = logging.getLogger(__name__)

ADMIN_ROLES: frozenset[UserRole] = frozenset({UserRole.ADMIN})
EVERYONE: frozenset[UserRole] = frozenset(UserRole) - {UserRole.SUPER_ADMIN}

_OPEN_DPR = (
    DprRequestStatus.RECEIVED,
    DprRequestStatus.VERIFYING_IDENTITY,
    DprRequestStatus.IN_PROGRESS,
    DprRequestStatus.ESCALATED,
)


def _person(emp: Employee | None) -> str | None:
    if emp is None:
        return None
    return emp.display_name or " ".join(p for p in (emp.first_name, emp.last_name) if p)


def _money(value: Any) -> str:
    return f"₹{Decimal(str(value)):,.2f}"


def _days(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return f"{text} day" if text == "1" else f"{text} days"


def _span(start: date, end: date) -> str:
    if start == end:
        return f"{start:%d %b %Y}"
    return f"{start:%d %b} to {end:%d %b %Y}"


def _not_own(stmt: Select, column: Any, user: User) -> Select:
    """Nobody decides a request about themselves."""
    if user.employee_id is not None:
        stmt = stmt.where(column != user.employee_id)
    return stmt


def _humanise(value: str) -> str:
    return value.replace("_", " ").capitalize()


# ── Sources ─────────────────────────────────────────────────────────────────


async def _leave(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    rows = await leave_service.approvals_inbox(session, user)
    return [
        {
            "id": r.id,
            "title": f"{r.leave_type_name}, {_span(r.start_date, r.end_date)}",
            "subject_name": r.employee_name,
            "detail": _days(r.total_days),
            "submitted_at": r.created_at,
            "due_on": r.start_date,
        }
        for r in rows
        if r.can_decide and r.employee_id != user.employee_id
    ]


async def _regularisations(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    stmt = tenant_scoped(
        select(AttendanceRegularisation, Employee)
        .join(Employee, Employee.id == AttendanceRegularisation.employee_id)
        .where(AttendanceRegularisation.status == RegularisationStatus.PENDING),
        AttendanceRegularisation,
        user,
    )
    if not permissions.is_hr(user):
        line = await permissions.reporting_line(session, user)
        if not line:
            return []
        stmt = stmt.where(AttendanceRegularisation.employee_id.in_(line))
    stmt = _not_own(stmt, AttendanceRegularisation.employee_id, user)
    rows = (await session.execute(stmt.order_by(AttendanceRegularisation.created_at))).all()
    return [
        {
            "id": reg.id,
            "title": f"Attendance correction for {reg.attendance_date:%d %b %Y}",
            "subject_name": _person(emp),
            "detail": f"Asks for: {_humanise(reg.requested_status.value)}",
            "submitted_at": reg.created_at,
        }
        for reg, emp in rows
    ]


async def _expense_claims(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    stmt = tenant_scoped(
        select(ExpenseClaim, Employee)
        .join(Employee, Employee.id == ExpenseClaim.employee_id)
        .where(ExpenseClaim.status == ExpenseClaimStatusEnum.SUBMITTED),
        ExpenseClaim,
        user,
    )
    stmt = _not_own(stmt, ExpenseClaim.employee_id, user)
    rows = (await session.execute(stmt.order_by(ExpenseClaim.created_at))).all()
    return [
        {
            "id": claim.id,
            "title": f"{_humanise(claim.category_key)} claim, {claim.incurred_on:%d %b %Y}",
            "subject_name": _person(emp),
            "detail": _money(claim.amount)
            + (" · flagged by policy" if claim.flagged_for_approver else ""),
            "submitted_at": claim.submitted_at or claim.created_at,
        }
        for claim, emp in rows
    ]


async def _travel(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    stmt = tenant_scoped(
        select(TravelRequest, Employee)
        .join(Employee, Employee.id == TravelRequest.employee_id)
        .where(TravelRequest.status == TravelRequestStatusEnum.SUBMITTED),
        TravelRequest,
        user,
    )
    stmt = _not_own(stmt, TravelRequest.employee_id, user)
    rows = (await session.execute(stmt.order_by(TravelRequest.created_at))).all()
    return [
        {
            "id": trip.id,
            "title": f"Travel, {_span(trip.from_date, trip.to_date)}",
            "subject_name": _person(emp),
            "detail": (trip.purpose or "")[:120],
            "submitted_at": trip.created_at,
            "due_on": trip.from_date,
        }
        for trip, emp in rows
    ]


async def _requisitions(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    stmt = tenant_scoped(
        select(Requisition).where(
            Requisition.status == RequisitionStatus.PENDING_APPROVAL,
            or_(
                Requisition.raised_by_user_id.is_(None),
                Requisition.raised_by_user_id != user.id,
            ),
        ),
        Requisition,
        user,
    )
    requisitions = (await session.scalars(stmt.order_by(Requisition.created_at))).all()
    if not requisitions:
        return []
    steps = (
        await session.scalars(
            tenant_scoped(
                select(RequisitionApproval)
                .where(
                    RequisitionApproval.requisition_id.in_([r.id for r in requisitions]),
                    RequisitionApproval.decision == ApprovalDecision.PENDING,
                )
                .order_by(RequisitionApproval.sequence),
                RequisitionApproval,
                user,
            )
        )
    ).all()
    first_step: dict[Any, RequisitionApproval] = {}
    for step in steps:
        first_step.setdefault(step.requisition_id, step)
    return [
        {
            "id": req.id,
            "title": f"Requisition {req.code}: {req.title}",
            "detail": (
                f"Step {first_step[req.id].sequence}: "
                f"{first_step[req.id].label or first_step[req.id].approver_role}"
                if req.id in first_step
                else f"{req.openings} opening(s)"
            ),
            "submitted_at": req.created_at,
            "due_on": req.target_close_date,
        }
        for req in requisitions
    ]


async def _offers(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    stmt = tenant_scoped(
        select(Offer).where(
            Offer.status == OfferStatus.PENDING_APPROVAL,
            or_(Offer.created_by_user_id.is_(None), Offer.created_by_user_id != user.id),
        ),
        Offer,
        user,
    )
    offers = (await session.scalars(stmt.order_by(Offer.created_at))).all()
    if not offers:
        return []
    steps = (
        await session.scalars(
            tenant_scoped(
                select(OfferApproval)
                .where(
                    OfferApproval.offer_id.in_([o.id for o in offers]),
                    OfferApproval.decision.in_(
                        [ApprovalDecision.PENDING, ApprovalDecision.VOIDED]
                    ),
                )
                .order_by(OfferApproval.sequence),
                OfferApproval,
                user,
            )
        )
    ).all()
    first_step: dict[Any, OfferApproval] = {}
    for step in steps:
        first_step.setdefault(step.offer_id, step)
    items = []
    for offer in offers:
        step = first_step.get(offer.id)
        detail = f"CTC {_money(offer.ctc_annual)}"
        if step is not None:
            detail += f" · step {step.sequence}: {step.label or step.approver_role}"
        items.append(
            {
                "id": offer.id,
                "title": f"Offer {offer.code}: {offer.designation}",
                "detail": detail,
                "submitted_at": offer.created_at,
                "due_on": offer.valid_until,
            }
        )
    return items


async def _lifecycle(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    stmt = tenant_scoped(
        select(LifecycleEvent, Employee)
        .join(Employee, Employee.id == LifecycleEvent.employee_id)
        .where(
            LifecycleEvent.status == LifecycleStatus.PENDING_APPROVAL,
            or_(
                LifecycleEvent.raised_by_user_id.is_(None),
                LifecycleEvent.raised_by_user_id != user.id,
            ),
        ),
        LifecycleEvent,
        user,
    )
    stmt = _not_own(stmt, LifecycleEvent.employee_id, user)
    rows = (await session.execute(stmt.order_by(LifecycleEvent.created_at))).all()
    return [
        {
            "id": event.id,
            "title": f"{_humanise(event.event_type.value)}, effective "
            f"{event.effective_from:%d %b %Y}",
            "subject_name": _person(emp),
            "detail": (event.reason or "")[:120],
            "submitted_at": event.created_at,
            "due_on": event.effective_from,
        }
        for event, emp in rows
    ]


async def _loans(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    stmt = tenant_scoped(
        select(LoanAccount, Employee)
        .join(Employee, Employee.id == LoanAccount.employee_id)
        .where(LoanAccount.status == LoanStatus.APPLIED),
        LoanAccount,
        user,
    )
    stmt = _not_own(stmt, LoanAccount.employee_id, user)
    rows = (await session.execute(stmt.order_by(LoanAccount.created_at))).all()
    return [
        {
            "id": loan.id,
            "title": f"Loan of {_money(loan.principal)}",
            "subject_name": _person(emp),
            "detail": f"{loan.tenure_months} months",
            "submitted_at": loan.applied_at or loan.created_at,
        }
        for loan, emp in rows
    ]


async def _settlements(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    stmt = tenant_scoped(
        select(Settlement, Employee)
        .join(Employee, Employee.id == Settlement.employee_id)
        .where(Settlement.status == SettlementStatus.PENDING_APPROVAL),
        Settlement,
        user,
    )
    stmt = _not_own(stmt, Settlement.employee_id, user)
    rows = (await session.execute(stmt.order_by(Settlement.created_at))).all()
    return [
        {
            "id": settlement.id,
            "title": "Full and final settlement",
            "subject_name": _person(emp),
            "detail": f"Net {_money(settlement.net_amount)}",
            "submitted_at": settlement.created_at,
        }
        for settlement, emp in rows
    ]


async def _retention_runs(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    stmt = tenant_scoped(
        select(RetentionRun).where(RetentionRun.status == RetentionRunStatus.AWAITING_APPROVAL),
        RetentionRun,
        user,
    )
    rows = (await session.scalars(stmt.order_by(RetentionRun.created_at))).all()
    return [
        {
            "id": run.id,
            "title": f"Retention run: {run.records_to_act} records to act on",
            "detail": f"{run.records_held} held under legal hold",
            "submitted_at": run.created_at,
        }
        for run in rows
    ]


async def _rights_requests(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    own = [pid for pid in (user.employee_id, user.id) if pid is not None]
    stmt = tenant_scoped(
        select(DataPrincipalRequest).where(
            DataPrincipalRequest.status.in_(_OPEN_DPR),
            DataPrincipalRequest.principal_id.not_in(own),
        ),
        DataPrincipalRequest,
        user,
    )
    rows = (await session.scalars(stmt.order_by(DataPrincipalRequest.due_by))).all()
    return [
        {
            "id": req.id,
            "title": f"{_humanise(req.request_type.value)} request (data protection)",
            "subject_name": req.principal_label,
            "detail": _humanise(req.status.value),
            "submitted_at": req.received_at,
            "due_on": req.due_by,
        }
        for req in rows
    ]


async def _agent_proposals(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    stmt = tenant_scoped(
        select(AgentProposal).where(
            AgentProposal.status == ProposalStatusEnum.PENDING,
            AgentProposal.expires_at > datetime.now(UTC),
        ),
        AgentProposal,
        user,
    )
    rows = (await session.scalars(stmt.order_by(AgentProposal.created_at))).all()
    return [
        {
            "id": row.id,
            "title": f"Agent proposal: {_humanise(row.action_key)}",
            "detail": f"Impact {row.impact} · confidence {Decimal(str(row.confidence)) * 100:.0f}%",
            "submitted_at": row.created_at,
            "due_on": row.expires_at.date(),
        }
        for row in rows
    ]


@dataclass(frozen=True, slots=True)
class Source:
    key: str
    label: str
    link: str
    roles: frozenset[UserRole]
    fetch: Callable[[AsyncSession, User], Awaitable[list[dict[str, Any]]]]
    module: str | None = None
    #: Anybody with a direct report also decides here, for their reporting line.
    line_managers: bool = False


#: Each role set is the gate on that module's decide endpoint.
SOURCES: tuple[Source, ...] = (
    Source("leave", "Time off", "/time", EVERYONE, _leave, module="leave"),
    # Comp-off approvers are line managers or HR; the Time page lists them for
    # anyone named.
    Source(
        "leave_comp_off", "Comp-off claims", "/time", permissions.HR_ROLES,
        comp_off_service.approval_items, module="leave", line_managers=True,
    ),
    Source(
        "attendance_regularisation",
        "Attendance corrections",
        "/time/attendance-review?tab=regularisations",
        permissions.HR_ROLES,
        _regularisations,
        module="leave",
        line_managers=True,
    ),
    Source(
        "expense_claim", "Expense claims", "/expenses/review", permissions.HR_ROLES,
        _expense_claims,
    ),
    Source("travel_request", "Travel requests", "/expenses/review", permissions.HR_ROLES, _travel),
    # The line manager's stage, before finance: the caller's own reporting line.
    Source(
        "expense_claim_manager", "Team expense claims", "/expenses/team",
        permissions.HR_ROLES, expense_inbox.manager_claim_items, line_managers=True,
    ),
    Source(
        "travel_request_manager", "Team travel requests", "/expenses/team",
        permissions.HR_ROLES, expense_inbox.manager_travel_items, line_managers=True,
    ),
    Source(
        "requisition", "Requisitions", "/hiring", permissions.HR_ROLES, _requisitions,
        module="hiring",
    ),
    Source("offer", "Offers", "/hiring/offers", permissions.HR_ROLES, _offers, module="hiring"),
    Source("lifecycle_event", "Lifecycle events", "/people/lifecycle", ADMIN_ROLES, _lifecycle),
    Source("loan", "Loans", "/payroll/loans", ADMIN_ROLES, _loans, module="payroll"),
    Source(
        "settlement", "Full & final settlements", "/payroll/settlements", ADMIN_ROLES,
        _settlements, module="payroll",
    ),
    Source(
        "payroll_run", "Payroll runs to approve", "/payroll", ADMIN_ROLES,
        payroll_inbox.run_approval_items, module="payroll",
    ),
    Source(
        "tax_proof", "Tax proofs to verify", "/payroll/tax-proofs", permissions.HR_ROLES,
        payroll_inbox.tax_proof_items, module="payroll",
    ),
    Source(
        "reimbursement_claim", "Reimbursement bills", "/payroll/loans", permissions.HR_ROLES,
        payroll_inbox.reimbursement_claim_items, module="payroll",
    ),
    Source(
        "retention_run", "Retention runs", "/compliance/retention", ADMIN_ROLES, _retention_runs
    ),
    Source(
        "data_principal_request", "Data protection requests", "/compliance",
        permissions.HR_ROLES, _rights_requests,
    ),
    Source("agent_proposal", "AI proposals", "/compliance/ai", permissions.HR_ROLES,
           _agent_proposals),
    Source(
        "asset_request", "Asset requests", "/assets/requests", permissions.HR_ROLES,
        assets_service.approvals_inbox, line_managers=True,
    ),
    Source(
        "profile_change", "Profile changes", "/people/change-requests", permissions.HR_ROLES,
        profile_service.inbox_items,
    ),
    Source(
        "document_verification", "Documents to verify", "/docs", permissions.HR_ROLES,
        documents_vault.pending_verification_items,
    ),
    Source(
        "resignation", "Resignations", "/people/exits", permissions.HR_ROLES,
        resignation_service.inbox_items, line_managers=True,
    ),
    Source(
        "clearance_task", "Clearance tasks", "/people/clearance", EVERYONE,
        clearance_tasks.inbox_items,
    ),
    Source(
        "feedback_request", "Feedback to give", "/performance/feedback?tab=give", EVERYONE,
        feedback_service.inbox_items,
    ),
)


async def inbox(session: AsyncSession, user: User) -> ApprovalInboxRead:
    """What is waiting on this person, oldest first, with a count per source."""
    tenant = await session.get(Tenant, user.tenant_id)
    modules = (tenant.enabled_modules or {}) if tenant else {}

    items: list[ApprovalItemRead] = []
    sources: list[ApprovalSourceRead] = []
    for source in SOURCES:
        if source.module and not modules.get(source.module):
            continue
        try:
            # The role gate on the module's own decide endpoint.
            permissions.require_role(
                user, source.roles, detail=source.label, line_managers=source.line_managers
            )
        except HTTPException:
            continue
        try:
            # A savepoint per source: one queue failing (a table not yet
            # migrated, a bad row) must not abort the transaction and take the
            # whole inbox — and the home screen that counts it — down with it.
            async with session.begin_nested():
                found = await source.fetch(session, user)
        except HTTPException as exc:
            sources.append(
                ApprovalSourceRead(
                    key=source.key, label=source.label, count=0, available=False,
                    note=str(exc.detail),
                )
            )
            continue
        except SQLAlchemyError:
            logger.exception("Approvals source %s could not be read", source.key)
            sources.append(
                ApprovalSourceRead(
                    key=source.key, label=source.label, count=0, available=False,
                    note="This queue could not be read just now.",
                )
            )
            continue
        for entry in found:
            items.append(
                ApprovalItemRead(source=source.key, source_label=source.label, link=source.link,
                                 **entry)
            )
        sources.append(ApprovalSourceRead(key=source.key, label=source.label, count=len(found)))

    items.sort(key=lambda item: item.submitted_at)
    return ApprovalInboxRead(items=items, sources=sources, total=len(items))
