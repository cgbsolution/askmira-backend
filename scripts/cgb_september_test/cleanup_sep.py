"""Remove the September 2026 payroll test data that seed_sep.py added.

    cd C:/cgbcode/askmira/backend
    .venv/Scripts/python.exe <this file> --db local
    .venv/Scripts/python.exe <this file> --db live --apply

Only what the seed made goes: punches from device SEED-SEP26 carrying the seed
tag, leave requests whose reason is the seed's, and the opening balances it
set. Approved and pending requests are cancelled through the leave service
(balance returned, attendance recomputed); declined ones are left as history.
September is then recomputed, so the days go back to what punches say.
Kept: the leave types, the Casual Leave policy and the work-week setting.
Refuses while September attendance is locked: unlock it first.
Notifications stay off. The audit log keeps every step, as it must.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import seed_sep  # noqa: E402  (notify off, identifiers never written back)
from sqlalchemy import delete, select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.deps import tenant_scoped  # noqa: E402
from app.models.attendance import AttendancePunch  # noqa: E402
from app.models.audit import AuditAction  # noqa: E402
from app.models.employee import Employee  # noqa: E402
from app.models.leave import (  # noqa: E402
    LeaveBalanceAdjustment,
    LeaveRequest,
    LeaveRequestStatus,
    LeaveType,
)
from app.models.tenant import Tenant  # noqa: E402
from app.models.user import User, UserRole  # noqa: E402
from app.schemas.leave import BalanceAdjustmentCreate  # noqa: E402
from app.services import audit  # noqa: E402
from app.services.attendance import service as attendance  # noqa: E402
from app.services.leave import ledger  # noqa: E402
from app.services.leave import service as leave  # noqa: E402

START, END = seed_sep.START, seed_sep.END


async def main(args: argparse.Namespace) -> int:
    url = seed_sep.LOCAL_URL if args.db == "local" else settings.DATABASE_URL
    if args.db == "live" and not args.apply:
        print("Live database: add --apply to remove the test data. Nothing done.")
        return 1
    engine = create_async_engine(url, poolclass=NullPool)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        tenant = await session.scalar(select(Tenant).where(Tenant.slug == seed_sep.WORKSPACE))
        actor = await session.scalar(
            select(User)
            .join(Employee, Employee.id == User.employee_id)
            .where(
                User.tenant_id == tenant.id,
                User.role == UserRole.ADMIN,
                Employee.employee_code == "EMP0001",
            )
        )
        if await attendance.active_lock_covering(session, actor, START, END) is not None:
            print("September attendance is locked. Unlock it (Attendance > Lock periods) first.")
            return 1

        # 1. Leave the seed raised: cancel what is open, through the service.
        requests = (
            await session.scalars(
                tenant_scoped(
                    select(LeaveRequest).where(LeaveRequest.reason == seed_sep.REASON),
                    LeaveRequest,
                    actor,
                )
            )
        ).all()
        ids = [(r.id, r.status) for r in requests]
        cancelled = 0
        for request_id, status in ids:
            if status in (LeaveRequestStatus.PENDING, LeaveRequestStatus.APPROVED):
                await leave.cancel(session, actor, request_id)
                cancelled += 1
        print(f"Leave: {cancelled} request(s) cancelled, {len(ids) - cancelled} declined kept")

        # 2. Opening balances the seed set go back to nothing.
        cl = await session.scalar(
            tenant_scoped(select(LeaveType).where(LeaveType.code == "CL"), LeaveType, actor)
        )
        reset = 0
        if cl is not None:
            seeded = (
                await session.execute(
                    tenant_scoped(
                        select(LeaveBalanceAdjustment.employee_id)
                        .where(
                            LeaveBalanceAdjustment.leave_type_id == cl.id,
                            LeaveBalanceAdjustment.reason.contains(seed_sep.REASON),
                        )
                        .distinct(),
                        LeaveBalanceAdjustment,
                        actor,
                    )
                )
            ).scalars().all()
            for employee_id in seeded:
                row = await leave.ensure_ledger_row(
                    session, actor, employee_id, cl.id, seed_sep.YEAR
                )
                if Decimal(str(row.opening_balance)) == 0:
                    continue
                await ledger.adjust(
                    session,
                    actor,
                    BalanceAdjustmentCreate(
                        employee_id=employee_id,
                        leave_type_id=cl.id,
                        year=seed_sep.YEAR,
                        kind="opening",
                        days=Decimal(0),
                        reason="Removing the September 2026 payroll test balance",
                    ),
                )
                reset += 1
        print(f"Balances: {reset} opening balance(s) set back to 0")

        # 3. Punches the seed imported, with their audit row in one commit.
        await session.refresh(actor)
        removed = (
            await session.execute(
                delete(AttendancePunch)
                .where(
                    AttendancePunch.tenant_id == tenant.id,
                    AttendancePunch.device_id == seed_sep.DEVICE,
                    AttendancePunch.raw_payload["seed"].astext == seed_sep.SEED,
                )
                .returning(AttendancePunch.id)
            )
        ).scalars().all()
        await audit.record(
            session,
            tenant_id=tenant.id,
            action=AuditAction.DELETE,
            entity_type="attendance_punch",
            actor=actor,
            changes={"device_id": seed_sep.DEVICE, "seed": seed_sep.SEED, "removed": len(removed)},
            summary=f"Removed {len(removed)} September 2026 test punch(es)",
        )
        await session.commit()
        print(f"Punches: {len(removed)} removed")

        # 4. September again, from what is left.
        await session.refresh(actor)
        employees = (
            await session.scalars(
                tenant_scoped(
                    select(Employee.id).where(Employee.deleted_at.is_(None)), Employee, actor
                )
            )
        ).all()
        written = await attendance.recompute_range(
            session, actor, employee_ids=list(employees), start=START, end=END
        )
        print(f"Recomputed {written} day(s) at {datetime.now(UTC):%H:%M} UTC")
    await engine.dispose()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", choices=["local", "live"], required=True)
    parser.add_argument("--apply", action="store_true")
    raise SystemExit(asyncio.run(main(parser.parse_args())))
