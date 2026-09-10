"""Referral programmes, referrals, and recruitment cost inputs (slice 36).

═══════════════════════════════════════════════════════════════════════════════
THE FUNNEL HAS NO TABLES, DELIBERATELY
═══════════════════════════════════════════════════════════════════════════════

Funnel counts, stage timings, source effectiveness and interviewer signals are
**derived** from applications, interviews and offers. Materialising them into a
summary table is how a dashboard comes to disagree with the records it was built
from — the summary is written once and the record moves afterwards, and a
recruiter who spots the discrepancy stops believing the dashboard.

So slice 36's tables are only the things that genuinely have no home: the
referral programme, referrals themselves, their payouts, and the cost inputs
that no other module records.

═══════════════════════════════════════════════════════════════════════════════
`referrals.candidate_key` IS WHAT DUPLICATES ARE DECIDED ON
═══════════════════════════════════════════════════════════════════════════════

Normalised at submission, and indexed, so "has anybody already referred this
person" is one lookup rather than a scan. First submission wins by
`submitted_at`, and the rule is in the programme terms an employee reads before
they refer.

═══════════════════════════════════════════════════════════════════════════════
A PAYOUT ROW EXISTS BEFORE IT IS PAID
═══════════════════════════════════════════════════════════════════════════════

The whole schedule is written when the candidate joins, with `paid_at` null.
That way the ninety-day milestone is visible as owed from day one, rather than
appearing only when somebody remembers to create it — which is how a referral
bonus gets half-paid and nobody notices.
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
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class ReferralStatusEnum(str, enum.Enum):
    SUBMITTED = "submitted"
    #: Another employee referred the same candidate first.
    DUPLICATE = "duplicate"
    IN_PROCESS = "in_process"
    HIRED = "hired"
    NOT_SELECTED = "not_selected"
    #: The referrer stopped being eligible before the payout. Distinct from the
    #: candidate not being selected, and the referrer is told which.
    INELIGIBLE = "ineligible"


class ShareChannelEnum(str, enum.Enum):
    WHATSAPP = "whatsapp"
    LINKEDIN = "linkedin"
    EMAIL = "email"
    COPY = "copy"


class PayoutStatusEnum(str, enum.Enum):
    #: Written when the candidate joins, before anything is paid. The ninety-day
    #: milestone is visible as owed from day one.
    SCHEDULED = "scheduled"
    PAID = "paid"
    #: The referrer left, or the hire did, before the milestone.
    FORFEITED = "forfeited"


class CostComponentEnum(str, enum.Enum):
    JOB_BOARD_SPEND = "job_board_spend"
    AGENCY_FEES = "agency_fees"
    REFERRAL_BONUSES = "referral_bonuses"
    RECRUITER_TIME = "recruiter_time"


class ReferralProgramme(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """A referral scheme: who may refer, for what, and what it pays.

    `payout_milestones` must total 100% of the bonus. An employee paid 80% of
    what they were promised will notice, and one paid 120% is a reconciliation
    nobody spots.
    """

    __tablename__ = "referral_programmes"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_referral_programme_name"),
    )

    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Empty means every open role.
    eligible_roles: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    #: `{grade: amount}`.
    bonus_by_grade: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    #: `[{key, label, share_percent, days_after_joining}]`, summing to 100.
    payout_milestones: Mapped[list] = mapped_column(
        JSONB, default=list, nullable=False
    )
    require_confirmed: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    exclude_on_pip: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    #: Never configurable to False. A panel member referring into their own
    #: requisition is deciding on a candidate they are paid for, and no tenant
    #: setting should be able to permit that.
    exclude_hiring_team: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Referral(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One referral, with the key duplicates are decided on."""

    __tablename__ = "referrals"
    __table_args__ = (
        Index("ix_referrals_candidate", "tenant_id", "candidate_key"),
        Index("ix_referrals_referrer", "tenant_id", "referrer_employee_id"),
        CheckConstraint(
            "bonus_amount >= 0", name="ck_referral_bonus_nonneg"
        ),
    )

    programme_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("referral_programmes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    referrer_employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False,
    )
    requisition_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("requisitions.id", ondelete="SET NULL"),
        nullable=True,
    )
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("candidates.id", ondelete="SET NULL"),
        nullable=True,
    )
    #: Normalised at submission, so "has anybody already referred this person"
    #: is one indexed lookup rather than a scan over spellings of an email.
    candidate_key: Mapped[str] = mapped_column(String(320), nullable=False)
    share_channel: Mapped[ShareChannelEnum | None] = mapped_column(
        Enum(ShareChannelEnum, name="referral_share_channel"), nullable=True
    )
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    status: Mapped[ReferralStatusEnum] = mapped_column(
        Enum(ReferralStatusEnum, name="referral_status"),
        default=ReferralStatusEnum.SUBMITTED,
        nullable=False,
    )
    #: Where this one lost the duplicate race, which referral won it. Stored so
    #: the referrer can be told who and when rather than only that they lost.
    duplicate_of_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )
    ineligibility_reason: Mapped[str | None] = mapped_column(
        String(48), nullable=True
    )
    bonus_amount: Mapped[float] = mapped_column(
        Numeric(14, 2), default=0, nullable=False
    )
    hired_on: Mapped[date | None] = mapped_column(Date, nullable=True)


class ReferralPayout(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One milestone of one referral bonus.

    Written when the candidate joins, with `paid_at` null, so the ninety-day
    milestone is visible as owed from day one rather than appearing when
    somebody remembers to create it.
    """

    __tablename__ = "referral_payouts"
    __table_args__ = (
        UniqueConstraint(
            "referral_id", "milestone_key", name="uq_payout_per_milestone"
        ),
        CheckConstraint("amount > 0", name="ck_payout_amount_positive"),
    )

    referral_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("referrals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    milestone_key: Mapped[str] = mapped_column(String(48), nullable=False)
    milestone_label: Mapped[str] = mapped_column(String(120), nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    payable_on: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    status: Mapped[PayoutStatusEnum] = mapped_column(
        Enum(PayoutStatusEnum, name="referral_payout_status"),
        default=PayoutStatusEnum.SCHEDULED,
        nullable=False,
    )
    #: Set when payroll has taken it. Null means owed, which is what keeps an
    #: unpaid milestone visible.
    payroll_reference: Mapped[str | None] = mapped_column(String(120), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    forfeited_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class RecruitmentCost(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One recorded recruitment cost, for cost per hire.

    Recorded per period and component rather than derived, because job-board
    spend and agency fees live in an accounting system this product does not
    read. A component with no row is *missing*, not zero, and the cost per hire
    says so.
    """

    __tablename__ = "recruitment_costs"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "period_label",
            "component",
            "source",
            name="uq_recruitment_cost_line",
        ),
        CheckConstraint("amount >= 0", name="ck_recruitment_cost_nonneg"),
    )

    period_label: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    component: Mapped[CostComponentEnum] = mapped_column(
        Enum(CostComponentEnum, name="recruitment_cost_component"), nullable=False
    )
    #: Optional: which job board, which agency. Lets source effectiveness carry
    #: a cost per hire per source.
    source: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(16, 2), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    recorded_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class CandidateDiversityDisclosure(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """One attribute a candidate chose to disclose, under consent.

    Separate from the candidate record, because it is collected on a different
    lawful basis and has to be deletable on its own: withdrawing consent to
    diversity monitoring must not delete the application.

    Absence of a row means they did not disclose, which is a different thing
    from any value the attribute can take. It is never imputed.
    """

    __tablename__ = "candidate_diversity_disclosures"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "candidate_id", "dimension", name="uq_disclosure_per_dimension"
        ),
    )

    candidate_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("candidates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    dimension: Mapped[str] = mapped_column(String(48), nullable=False)
    value: Mapped[str] = mapped_column(String(80), nullable=False)
    #: The consent record this disclosure rests on. Without it the row should
    #: not exist.
    consent_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )
    disclosed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
