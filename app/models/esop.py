"""Employee stock options: plans, grants, vesting, valuations and exercises.

A **grant** gives a person a number of options (or units) at an exercise price,
vesting on a schedule — a cliff, then equal tranches every few months until
the whole grant has vested. The schedule is stored on the grant as it was
granted, so a later change to the plan's defaults never rewrites a promise.

A **valuation** is the fair market value per unit from a date, as the company's
valuer set it. Projections read the latest one; nothing here invents a price.

An **exercise** turns vested options into shares. Its perquisite — units times
(fair market value on the exercise date minus exercise price) — is taxable
salary, so payroll adds it to the person's taxable income in the next run, as
a non-cash perquisite.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class EsopInstrument(str, enum.Enum):
    OPTIONS = "options"
    RSU = "rsu"
    SAR = "sar"
    PHANTOM = "phantom"


class GrantStatus(str, enum.Enum):
    ACTIVE = "active"
    CANCELLED = "cancelled"


class EsopPlan(Base, UUIDMixin, TenantMixin, TimestampMixin):
    __tablename__ = "esop_plans"
    __table_args__ = (CheckConstraint("pool_units > 0", name="ck_esop_plan_pool"),)

    name: Mapped[str] = mapped_column(String(160), nullable=False)
    instrument: Mapped[EsopInstrument] = mapped_column(
        Enum(EsopInstrument, name="esop_instrument"), nullable=False
    )
    pool_units: Mapped[int] = mapped_column(Integer, nullable=False)
    default_cliff_months: Mapped[int] = mapped_column(Integer, default=12, nullable=False)
    default_vesting_months: Mapped[int] = mapped_column(Integer, default=48, nullable=False)
    default_frequency_months: Mapped[int] = mapped_column(Integer, default=3, nullable=False)


class EsopGrant(Base, UUIDMixin, TenantMixin, TimestampMixin):
    __tablename__ = "esop_grants"
    __table_args__ = (
        CheckConstraint("units > 0", name="ck_esop_grant_units"),
        CheckConstraint("exercise_price >= 0", name="ck_esop_grant_price"),
        CheckConstraint(
            "frequency_months > 0 AND vesting_months >= cliff_months",
            name="ck_esop_grant_schedule",
        ),
    )

    plan_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("esop_plans.id", ondelete="RESTRICT", name="fk_esop_grants_plan"),
        nullable=False,
        index=True,
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="CASCADE", name="fk_esop_grants_employee"),
        nullable=False,
        index=True,
    )
    grant_date: Mapped[date] = mapped_column(Date, nullable=False)
    units: Mapped[int] = mapped_column(Integer, nullable=False)
    exercise_price: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    vesting_start: Mapped[date] = mapped_column(Date, nullable=False)
    cliff_months: Mapped[int] = mapped_column(Integer, nullable=False)
    vesting_months: Mapped[int] = mapped_column(Integer, nullable=False)
    frequency_months: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[GrantStatus] = mapped_column(
        Enum(GrantStatus, name="esop_grant_status"), default=GrantStatus.ACTIVE, nullable=False
    )
    #: Set when cancelled: vesting stops here; what had vested stays.
    cancelled_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class EsopValuation(Base, UUIDMixin, TenantMixin, TimestampMixin):
    """Fair market value per unit from a date. Never edited; a new one supersedes."""

    __tablename__ = "esop_valuations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "plan_id", "effective_from", name="uq_esop_valuation_from"),
        CheckConstraint("fmv_per_unit >= 0", name="ck_esop_valuation_fmv"),
    )

    plan_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("esop_plans.id", ondelete="CASCADE", name="fk_esop_valuations_plan"),
        nullable=False,
        index=True,
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    fmv_per_unit: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    #: The valuer and the report it came from.
    source: Mapped[str] = mapped_column(String(300), nullable=False)


class EsopExercise(Base, UUIDMixin, TenantMixin, TimestampMixin):
    __tablename__ = "esop_exercises"
    __table_args__ = (CheckConstraint("units > 0", name="ck_esop_exercise_units"),)

    grant_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("esop_grants.id", ondelete="RESTRICT", name="fk_esop_exercises_grant"),
        nullable=False,
        index=True,
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="CASCADE", name="fk_esop_exercises_employee"),
        nullable=False,
        index=True,
    )
    exercise_date: Mapped[date] = mapped_column(Date, nullable=False)
    units: Mapped[int] = mapped_column(Integer, nullable=False)
    fmv_per_unit: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    exercise_price: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    #: units x (fmv - exercise price), never below zero: taxable as salary.
    perquisite_value: Mapped[Decimal] = mapped_column(Numeric(16, 2), nullable=False)
    #: "payroll_run:<id>" once a run has taxed it.
    payroll_reference: Mapped[str | None] = mapped_column(String(64), nullable=True)
