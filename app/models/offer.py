"""Offers, requisition approval and the conversion to employee.

`Offer` carries the terms; `OfferSalaryComponent` carries the breakup that was
shown on the letter. That breakup is written from `resolve_ctc` — the same
function payroll calls — and never recomputed, so the offer letter and the first
payslip cannot disagree. A mismatch there is a trust incident on somebody's
first day and it is common in the market.

`OfferApproval.terms_fingerprint` is what makes an approval an approval *of
specific terms*. Change the CTC afterwards and the fingerprint stops matching,
the approval voids, and it goes back round.

`OfferEvent` is the status history. Offer-drop — accepted, then did not join —
is a material metric in the Indian market, and a reason that lives only in a
recruiter's memory cannot be reported on by department, recruiter or notice
period.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class OfferStatus(str, enum.Enum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    RELEASED = "released"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    REVOKED = "revoked"
    LAPSED = "lapsed"
    JOINED = "joined"
    # Accepted and then did not turn up. Distinct from DECLINED, because the two
    # cost the company completely different amounts and want different fixes.
    DROPPED = "dropped"


#: Statuses after which the vacancy is no longer held by this candidate.
#: A revoked or dropped offer must put the opening back, or the requisition
#: quietly shows as filled while the seat is empty.
VACANCY_RELEASING_STATUSES = frozenset(
    {OfferStatus.DECLINED, OfferStatus.REVOKED, OfferStatus.LAPSED, OfferStatus.DROPPED}
)


class ApprovalDecision(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    # The offer was materially edited under this approver. Not a rejection:
    # nobody decided against anything, the decision simply no longer applies.
    VOIDED = "voided"


class ApprovalScope(str, enum.Enum):
    REQUISITION = "requisition"
    OFFER = "offer"


class OfferComponentKind(str, enum.Enum):
    """Offer-level money that is not monthly salary.

    Real parts of what was agreed, so they are material terms and they appear on
    the letter — but they are not payslip components and modelling them as such
    would put a joining bonus into every month's PF base.
    """

    JOINING_BONUS = "joining_bonus"
    RETENTION_BONUS = "retention_bonus"
    VARIABLE_PAY = "variable_pay"
    ESOP = "esop"
    RELOCATION = "relocation"


class ApprovalChainRule(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One band in a configurable approval chain.

    `ctc_max` is exclusive so adjacent bands meet exactly, and the rule set is
    validated to cover every CTC — a gap resolves to an empty chain, which is an
    offer released on nobody's signature.
    """

    __tablename__ = "approval_chain_rules"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "scope", "sequence", name="uq_approval_rule_sequence"
        ),
        CheckConstraint(
            "ctc_max IS NULL OR ctc_max > ctc_min", name="ck_approval_rule_band"
        ),
    )

    scope: Mapped[ApprovalScope] = mapped_column(
        Enum(ApprovalScope, name="approval_scope"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    approver_role: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str | None] = mapped_column(String(128), nullable=True)

    ctc_min: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    # NULL is the unbounded top band, which every rule set must have.
    ctc_max: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    # Empty list = every grade.
    grades: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)

    # A step that exists only to catch offers above the approved band. It adds a
    # person rather than a warning colour, because approvers approve.
    only_when_over_band: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class RequisitionApproval(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One signature on a requisition."""

    __tablename__ = "requisition_approvals"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "requisition_id", "sequence", name="uq_requisition_approval_step"
        ),
    )

    requisition_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("requisitions.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    approver_role: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Why this step exists. "You are step 3 of 4" tells an approver nothing.
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    decision: Mapped[ApprovalDecision] = mapped_column(
        Enum(ApprovalDecision, name="approval_decision"),
        default=ApprovalDecision.PENDING, nullable=False, index=True,
    )
    decided_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # What was flagged to this approver at the moment they decided. Snapshotted,
    # so "did they see the headcount breach" is answerable later.
    flags_shown: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)


class Offer(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """An offer to one candidate against one requisition."""

    __tablename__ = "offers"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_offer_code"),
        Index("ix_offers_status_valid", "tenant_id", "status", "valid_until"),
        CheckConstraint("ctc_annual > 0", name="ck_offer_ctc_positive"),
    )

    code: Mapped[str] = mapped_column(String(32), nullable=False)

    candidate_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("candidates.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )
    requisition_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("requisitions.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )

    # ── Terms ───────────────────────────────────────────────────────────────
    # The same structure payroll will run. Not a copy of it, not an
    # offer-specific variant: the identity is what makes the letter and the
    # first payslip agree.
    salary_structure_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("salary_structures.id", ondelete="RESTRICT"),
        nullable=False,
    )
    ctc_annual: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="INR", nullable=False)

    joining_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    joining_location_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("locations.id", ondelete="SET NULL"), nullable=True
    )
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("departments.id", ondelete="SET NULL"), nullable=True
    )
    legal_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("legal_entities.id", ondelete="SET NULL"),
        nullable=True,
    )
    grade: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    designation: Mapped[str] = mapped_column(String(255), nullable=False)
    probation_months: Mapped[int] = mapped_column(Integer, default=6, nullable=False)
    notice_period_days: Mapped[int] = mapped_column(Integer, default=60, nullable=False)

    # ── Lifecycle ───────────────────────────────────────────────────────────
    status: Mapped[OfferStatus] = mapped_column(
        Enum(OfferStatus, name="offer_status"),
        default=OfferStatus.DRAFT, nullable=False, index=True,
    )
    valid_until: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    released_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    responded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Why the offer ended the way it did. Required by the service for every
    # terminal status that is not a plain acceptance — a drop reason nobody
    # recorded cannot be reported on by department or recruiter.
    outcome_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The candidate said they had already resigned elsewhere. Carries legal and
    # reputational exposure and is surfaced to HR rather than logged.
    resigned_elsewhere: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )

    letter_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("issued_letters.id", ondelete="SET NULL"),
        nullable=True,
    )
    converted_employee_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("employees.id", ondelete="SET NULL"), nullable=True
    )

    # The budget deviation and parity view computed when the offer went for
    # approval, kept so the approval record shows what the approver was shown.
    approval_context: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    #: How the candidate accepted, when they did it themselves in the portal:
    #: the name they typed, when, from where, and the hash of the letter they
    #: were shown - so "I never accepted that version" has an answer.
    acceptance_record: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    components: Mapped[list[OfferComponent]] = relationship(
        back_populates="offer", cascade="all, delete-orphan", lazy="selectin"
    )
    salary_components: Mapped[list[OfferSalaryComponent]] = relationship(
        back_populates="offer", cascade="all, delete-orphan", lazy="selectin"
    )

    def __repr__(self) -> str:
        return f"<Offer {self.code} {self.status.value}>"


class OfferComponent(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """Joining bonus, retention bonus, variable pay, ESOP, relocation.

    Part of what was agreed and therefore a material term, but never a monthly
    salary component: putting a joining bonus into the structure would fold it
    into every month's PF base.
    """

    __tablename__ = "offer_components"

    offer_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("offers.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[OfferComponentKind] = mapped_column(
        Enum(OfferComponentKind, name="offer_component_kind"), nullable=False
    )
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    # "on joining", "after 12 months", "vesting over 4 years".
    payable_terms: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Whether the amount counts toward the headline CTC on the letter. A
    # variable component usually does; a signing bonus usually does not, and
    # getting this wrong is how an offer reads richer than it pays.
    counts_toward_ctc: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )

    offer: Mapped[Offer] = relationship(back_populates="components")


class OfferSalaryComponent(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """The breakup exactly as the offer letter showed it.

    Written from `resolve_ctc` — payroll's own function — and never recomputed,
    so what the candidate was promised is comparable field by field against what
    the first payslip pays.
    """

    __tablename__ = "offer_salary_components"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "offer_id", "component_code", name="uq_offer_component_once"
        ),
    )

    offer_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("offers.id", ondelete="CASCADE"), nullable=False
    )
    component_code: Mapped[str] = mapped_column(String(32), nullable=False)
    component_name: Mapped[str] = mapped_column(String(128), nullable=False)
    annual_amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    monthly_amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    part_of_ctc: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    offer: Mapped[Offer] = relationship(back_populates="salary_components")


class OfferApproval(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One signature on an offer, against specific terms.

    `terms_fingerprint` is the whole point: an approval is a decision about a
    number, and if the number changes the decision is being reused as cover for
    something nobody saw.
    """

    __tablename__ = "offer_approvals"
    __table_args__ = (
        UniqueConstraint("tenant_id", "offer_id", "sequence", name="uq_offer_approval_step"),
    )

    offer_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("offers.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    approver_role: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    decision: Mapped[ApprovalDecision] = mapped_column(
        Enum(ApprovalDecision, name="approval_decision", create_type=False),
        default=ApprovalDecision.PENDING, nullable=False, index=True,
    )
    decided_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # SHA-256 of the material terms as they stood when this approver decided.
    # Stops matching the moment the offer is materially edited.
    terms_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Named terms that changed and voided this approval, so re-approval says
    # "CTC changed" rather than "this offer was edited".
    voided_by_changes: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)

    # The deviation and parity view this approver was shown.
    context_shown: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)


class OfferEvent(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """Insert-only status history.

    Offer-drop rate by department, recruiter and notice-period length is a
    material metric in this market (slice 36). It can only be computed if every
    transition, and its reason, was written down at the time.
    """

    __tablename__ = "offer_events"

    offer_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("offers.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    from_status: Mapped[OfferStatus | None] = mapped_column(
        Enum(OfferStatus, name="offer_status", create_type=False), nullable=True
    )
    to_status: Mapped[OfferStatus] = mapped_column(
        Enum(OfferStatus, name="offer_status", create_type=False),
        nullable=False, index=True,
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    # Snapshotted so a drop-rate report by notice period does not depend on the
    # candidate record still being accurate years later.
    facts: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
