"""Grades and their pay bands.

A grade is the level a role sits at (`L1`…`L8`, `M1`…), and its band is the
annual CTC range the company pays for it: a minimum, a midpoint and a maximum.
`Employee.grade` holds the grade's code. Once a workspace defines its grades,
a record can only carry one of them, and each person's pay can be read against
the band — the compa-ratio, CTC over the midpoint — without anybody keeping a
spreadsheet beside the HRMS.

Money is `Numeric`. A band edit is audited with its before and after.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import Boolean, CheckConstraint, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class PayGrade(Base, UUIDMixin, TenantMixin, TimestampMixin):
    __tablename__ = "pay_grades"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_pay_grades_tenant_code"),
        CheckConstraint(
            "band_min <= band_mid AND band_mid <= band_max", name="ck_pay_grade_band_order"
        ),
        CheckConstraint("band_min >= 0", name="ck_pay_grade_band_non_negative"),
    )

    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    #: Higher is more senior. Orders the grades; two may share a level.
    level: Mapped[int] = mapped_column(Integer, nullable=False)
    band_min: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    band_mid: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    band_max: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="INR", nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
