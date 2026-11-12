"""Casual Leave at 1.25 days a month from November 2026, and no leave during probation.

Asked for on 2026-11-13 for CGB and Artbox:

* Casual Leave (CL) is 1.25 days a month. 2026 starts with November and
  December, so everybody gets 2.5 days for the rest of the year, credited now.
  From January 2027, 1.25 days land on the 1st of each month.
* Nobody earns leave in their first six months (probation). They show "On
  probation" in Time off → Team balances until then.

Done the way HR would in Time off → Setup, through the same services, audited
as the acting admin (`--as`, else the workspace's first admin):

1. a CL policy version from 2026-11-01: 15 days a year, credited up front
   (Nov + Dec = 2.5) — the current 0-day version closes on 2026-10-31;
2. a CL policy version from 2027-01-01: 15 days a year, credited monthly;
3. the leave setting "Probation before leave" = 6 months (admin only).

    cd backend
    # What would change, nothing saved:
    .venv/Scripts/python.exe scripts/set_casual_leave_and_probation.py --workspace cgb-solutions
    # Do it:
    ... --apply

Each step is skipped when it is already there, so a rerun changes nothing.
The database is the one in DATABASE_URL; its host is printed before anything runs.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import defer
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.models.employee import Employee, EmploymentStatus
from app.models.leave import LeavePolicy, LeaveType
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.schemas.leave import LeavePolicyCreate, LeaveSettingsUpdate
from app.services.leave import engine
from app.services.leave import service as leave_service

LEAVE_CODE = "CL"
YEARLY = Decimal(15)  # 1.25 a month
NOV = date(2026, 11, 1)
JAN = date(2027, 1, 1)
NOV_NAME = "Casual Leave - Nov & Dec 2026 (2.5 days)"
MONTHLY_NAME = "Casual Leave - 1.25 days a month"


async def _current_people(session: AsyncSession, tenant_id) -> list[Employee]:
    """Everybody not exited. Identifiers stay unread: they are encrypted with the
    server's key, which a laptop running this need not have."""
    return list(
        (
            await session.scalars(
                select(Employee)
                .options(
                    defer(Employee.pan),
                    defer(Employee.aadhaar_number),
                    defer(Employee.bank_account_number),
                )
                .where(
                    Employee.tenant_id == tenant_id,
                    Employee.deleted_at.is_(None),
                    Employee.status != EmploymentStatus.EXITED,
                )
            )
        ).all()
    )


async def _summary(session: AsyncSession, actor: User, leave_type: LeaveType, year: int) -> None:
    people = await _current_people(session, actor.tenant_id)
    lines = await leave_service.balance_lines(session, actor, people, year)
    figures: Counter[str] = Counter()
    waiting = 0
    for person in people:
        cl = [line for line in lines[person.id] if line.leave_type.id == leave_type.id]
        if not cl:
            figures["no CL line"] += 1
            continue
        figures[engine.days_text(cl[0].balance.available)] += 1
        waiting += cl[0].probation_until is not None
    shown = ", ".join(f"{count} with {value}" for value, count in sorted(figures.items()))
    print(f"  {year}: {len(people)} current people — CL available: {shown}")
    print(f"        on probation today: {waiting}")


def _projection(
    policies: list[LeavePolicy], people: list[Employee], months: int, today: date
) -> None:
    """What the new versions would credit, computed without saving anything."""
    figures: Counter[str] = Counter()
    for person in people:
        windows = engine.accrual_windows(
            policies,
            year=today.year,
            location_id=person.location_id,
            grade=person.grade,
            gender=leave_service.gender_of(person),
        )
        earned = engine.accrued_across(
            windows,
            year=today.year,
            as_of=today,
            joined_on=engine.accrual_start(person.date_of_joining, months),
        )
        figures[engine.days_text(earned)] += 1
    shown = ", ".join(f"{count} with {value}" for value, count in sorted(figures.items()))
    print(f"  would credit for {today.year} as of {today}: {shown}")


async def main(args: argparse.Namespace) -> int:
    url = make_url(settings.DATABASE_URL)
    print(f"Database: {url.host}:{url.port}/{url.database}")
    engine_ = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    try:
        async with AsyncSession(engine_, expire_on_commit=False) as session:
            tenant = await session.scalar(
                select(Tenant).where(Tenant.slug == args.workspace, Tenant.deleted_at.is_(None))
            )
            if tenant is None:
                print(f"No workspace {args.workspace}.")
                return 1
            actor_stmt = select(User).where(
                User.tenant_id == tenant.id,
                User.is_active.is_(True),
                User.role == UserRole.ADMIN,
            )
            if args.as_email:
                actor_stmt = actor_stmt.where(func.lower(User.email) == args.as_email.lower())
            actor = (await session.scalars(actor_stmt.order_by(User.created_at).limit(1))).first()
            if actor is None:
                print("No active admin to act as (probation is an admin setting). Pass --as.")
                return 1
            print(f"Workspace: {tenant.name} ({tenant.slug}); acting as {actor.email}")

            leave_type = await session.scalar(
                select(LeaveType).where(
                    LeaveType.tenant_id == tenant.id, LeaveType.code == LEAVE_CODE
                )
            )
            if leave_type is None or not leave_type.is_active:
                print(f"No active leave type {LEAVE_CODE}.")
                return 1
            policies = list(
                (
                    await session.scalars(
                        select(LeavePolicy)
                        .where(
                            LeavePolicy.tenant_id == tenant.id,
                            LeavePolicy.leave_type_id == leave_type.id,
                        )
                        .order_by(LeavePolicy.created_at)
                    )
                ).all()
            )
            print(f"\n{leave_type.name} ({leave_type.code}) versions now:")
            for p in policies:
                limited = (p.location_id, p.grade, p.applies_to_gender) != (None, None, None)
                print(
                    f"  {'active ' if p.is_active else 'retired'} {p.effective_from} to "
                    f"{p.effective_to or 'open'}: {p.name} — {p.annual_quota_days}/yr "
                    f"{p.accrual_frequency}, carry {p.max_carry_forward_days}"
                    f"{' (limited population)' if limited else ''}"
                )
            active = [p for p in policies if p.is_active]
            if any(
                p.location_id is not None or p.grade is not None or p.applies_to_gender is not None
                for p in active
            ):
                print("A CL version is limited to a location, grade or gender: set it up by hand.")
                return 1
            current = engine.select_policy(active, location_id=None, grade=None, on=NOV)
            has_nov = any(p.effective_from == NOV for p in active)
            has_jan = any(p.effective_from == JAN for p in active)
            months_now = await leave_service.probation_months(session, actor)

            print("\nPlan:")
            print(
                f"  1. {'already there' if has_nov else 'add'}: {NOV_NAME} from {NOV}, "
                f"{YEARLY}/yr credited up front"
            )
            print(
                f"  2. {'already there' if has_jan else 'add'}: {MONTHLY_NAME} from {JAN}, "
                f"{YEARLY}/yr monthly"
            )
            print(
                f"  3. probation before leave: {months_now} -> {args.probation_months} months"
                f"{' (no change)' if months_now == args.probation_months else ''}"
            )

            today = leave_service.local_today()
            print("\nBefore:")
            await _summary(session, actor, leave_type, today.year)

            def _version(name: str, frequency: str, start: date) -> LeavePolicyCreate:
                return LeavePolicyCreate(
                    leave_type_id=leave_type.id,
                    name=name,
                    annual_quota_days=YEARLY,
                    accrual_frequency=frequency,
                    max_carry_forward_days=engine.days(
                        current.max_carry_forward_days if current else 0
                    ),
                    max_consecutive_days=current.max_consecutive_days if current else None,
                    min_notice_days=current.min_notice_days if current else 0,
                    sandwich_weekends_holidays=bool(current and current.sandwich_weekends_holidays),
                    effective_from=start,
                )

            if not args.apply:
                people = await _current_people(session, tenant.id)
                # Unsaved copies of the versions the plan would leave in force.
                planned = [
                    LeavePolicy(**_version(NOV_NAME, "annual", NOV).model_dump(), is_active=True)
                ]
                planned[0].effective_to = date(2026, 12, 31)
                _projection(planned, people, args.probation_months, today)
                print("\nDry run: nothing saved. Add --apply to do it.")
                return 0

            if not has_nov:
                await leave_service.create_policy(session, actor, _version(NOV_NAME, "annual", NOV))
                print(f"Added {NOV_NAME}.")
            if not has_jan:
                await leave_service.create_policy(
                    session, actor, _version(MONTHLY_NAME, "monthly", JAN)
                )
                print(f"Added {MONTHLY_NAME}.")
            if months_now != args.probation_months:
                await leave_service.update_settings(
                    session, actor, LeaveSettingsUpdate(probation_months=args.probation_months)
                )
                print(f"Probation before leave set to {args.probation_months} months.")

            print("\nAfter:")
            for p in (
                await session.scalars(
                    select(LeavePolicy)
                    .where(
                        LeavePolicy.tenant_id == tenant.id,
                        LeavePolicy.leave_type_id == leave_type.id,
                        LeavePolicy.is_active.is_(True),
                    )
                    .order_by(LeavePolicy.effective_from)
                )
            ).all():
                print(f"  {p.effective_from} to {p.effective_to or 'open'}: {p.name}")
            await _summary(session, actor, leave_type, today.year)
            await _summary(session, actor, leave_type, today.year + 1)
            return 0
    finally:
        await engine_.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--probation-months", type=int, default=6)
    parser.add_argument("--as", dest="as_email")
    parser.add_argument("--apply", action="store_true")
    sys.exit(asyncio.run(main(parser.parse_args())))
