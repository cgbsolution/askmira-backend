"""Seed September 2026 attendance + leave for CGB Solutions, to test payroll.

    cd C:/cgbcode/askmira/backend
    .venv/Scripts/python.exe <this file> --db local          # rehearse on askmira_cgb_qa
    .venv/Scripts/python.exe <this file> --db live --apply   # the shared Supabase database

What it does, through the product's own services (each change audited):
  1. CGBiz Solutions' work week = the General shift (Mon-Fri, Sat/Sun off).
  2. Leave types Casual Leave (paid) and Leave Without Pay (unpaid); a Casual
     Leave policy with no automatic quota; opening balances for 7 people.
  3. Punches (source "import", device SEED-SEP26) for every weekday each person
     was at work, deterministic times, all inside the 15-minute grace.
  4. Leave requests: approved paid, approved unpaid, pending and declined.
  5. Recomputes 1-30 Sep for everybody employed in September.

Notifications are switched off for the whole run: no email, push or in-app
message reaches anybody. Re-running adds nothing (punches dedupe, leave and
balances are skipped when already there).
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import sys
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

BACKEND = Path("C:/cgbcode/askmira/backend")
sys.path.insert(0, str(BACKEND))

from fastapi import HTTPException  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.crypto import EncryptionError  # noqa: E402
from app.models import types as coltypes  # noqa: E402
from app.core.deps import tenant_scoped  # noqa: E402
from app.models.attendance import AttendanceDay, PunchDirection, Shift  # noqa: E402
from app.models.employee import Employee, EmploymentStatus  # noqa: E402
from app.models.leave import LeavePolicy, LeaveRequest, LeaveRequestStatus, LeaveType  # noqa: E402
from app.models.org import LegalEntity  # noqa: E402
from app.models.tenant import Tenant  # noqa: E402
from app.models.user import User, UserRole  # noqa: E402
from app.schemas.leave import (  # noqa: E402
    BalanceAdjustmentCreate,
    LeavePolicyCreate,
    LeaveRequestCreate,
    LeaveTypeCreate,
)
from app.schemas.org import LegalEntityUpdate  # noqa: E402
from app.services.attendance import service as attendance  # noqa: E402
from app.services.leave import ledger  # noqa: E402
from app.services.leave import service as leave  # noqa: E402
from app.services.notifications import dispatch  # noqa: E402

LOCAL_URL = "postgresql+asyncpg://askmira:askmira_dev@localhost:5441/askmira_cgb_qa"
WORKSPACE = "cgb-solutions"
START, END = date(2026, 9, 1), date(2026, 9, 30)
YEAR = 2026
DEVICE = "SEED-SEP26"
SEED = "september-2026-payroll-test"
REASON = "[Test data] September 2026 payroll check"
OUT = Path(__file__).resolve().parent


class _Unreadable(str):
    """An identifier encrypted with the server's key, which this machine lacks."""


_read_identifier = coltypes.EncryptedText.process_result_value
_write_identifier = coltypes.EncryptedText.process_bind_param


def _result(self, value, dialect):
    try:
        return _read_identifier(self, value, dialect)
    except EncryptionError:
        return _Unreadable("")


def _bind(self, value, dialect):
    if isinstance(value, _Unreadable):
        raise RuntimeError("Refusing to write an identifier this script could not read.")
    return _write_identifier(self, value, dialect)


# Bank, PAN and Aadhaar stay as they are: never read here, never written back.
coltypes.EncryptedText.process_result_value = _result  # type: ignore[method-assign]
coltypes.EncryptedText.process_bind_param = _bind  # type: ignore[method-assign]


async def _silent(*_a, **_k):
    return dispatch.DispatchOutcome()


# Nobody hears about any of this.
dispatch.notify = _silent  # type: ignore[assignment]


def d(day: int) -> date:
    return date(2026, 9, day)


@dataclass
class Leave:
    type: str  # "CL" | "LWP"
    start: int
    end: int
    outcome: str  # approved | pending | rejected
    half: str | None = None  # first_half | second_half
    try_cl_first: bool = False  # ask for Casual Leave first, expecting a refusal


@dataclass
class Plan:
    what: str
    absent: list[int] = field(default_factory=list)
    half: list[int] = field(default_factory=list)
    leaves: list[Leave] = field(default_factory=list)
    cl_balance: Decimal | None = None


# Everybody not named here is present every weekday.
PLANS: dict[str, Plan] = {
    "CBL22-044": Plan(
        "2 days Casual Leave, approved, balance 3", leaves=[Leave("CL", 10, 11, "approved")],
        cl_balance=Decimal(3),
    ),
    "CBL23-346": Plan("Absent 15 Sep, no leave applied", absent=[15]),
    "CBL25-744": Plan("Half day 8 and 22 Sep", half=[8, 22]),
    "CBL26-772": Plan(
        "No balance: 23-25 Sep Leave Without Pay, approved",
        leaves=[Leave("LWP", 23, 25, "approved")],
    ),
    "CBL26-774": Plan(
        "Absent 17-18 Sep; Casual Leave applied (balance 2) but still pending",
        leaves=[Leave("CL", 17, 18, "pending")], cl_balance=Decimal(2),
    ),
    "CBP25-693": Plan(
        "Half-day Casual Leave 16 Sep approved (worked first half); half day 29 Sep, no leave",
        half=[29], leaves=[Leave("CL", 16, 16, "approved", half="second_half")],
        cl_balance=Decimal(1),
    ),
    "CBP25-735": Plan("Absent Fri 4 and Mon 7 Sep around the weekend, no leave", absent=[4, 7]),
    "CDL23-474": Plan(
        "5 days Casual Leave 21-25 Sep, approved, balance 6",
        leaves=[Leave("CL", 21, 25, "approved")], cl_balance=Decimal(6),
    ),
    "CDL24-555": Plan(
        "Casual Leave 2 Sep applied but declined (balance 2) - absent",
        leaves=[Leave("CL", 2, 2, "rejected")], cl_balance=Decimal(2),
    ),
    "CDL25-696": Plan(
        "Absent 3 Sep (no leave), half day 14 Sep, Casual Leave 28 Sep approved (balance 1)",
        absent=[3], half=[14], leaves=[Leave("CL", 28, 28, "approved")], cl_balance=Decimal(1),
    ),
    "CDL25-725": Plan("Four half days: 1, 10, 17, 24 Sep", half=[1, 10, 17, 24]),
    "CDL25-749": Plan(
        "Half-day Leave Without Pay 9 Sep approved (worked first half); absent 30 Sep, no leave",
        absent=[30], leaves=[Leave("LWP", 9, 9, "approved", half="second_half")],
    ),
    "CDL26-784": Plan(
        "No balance: Casual Leave refused, so 14-15 Sep Leave Without Pay, approved",
        leaves=[Leave("LWP", 14, 15, "approved", try_cl_first=True)],
    ),
    "CDL26-789": Plan("Absent a whole week 7-11 Sep, no leave", absent=[7, 8, 9, 10, 11]),
    "CNK26-781": Plan(
        "Casual Leave 1 Sep approved (balance 1) + Leave Without Pay 2 Sep approved",
        leaves=[Leave("CL", 1, 1, "approved"), Leave("LWP", 2, 2, "approved")],
        cl_balance=Decimal(1),
    ),
}


def _jitter(code: str, day: date, salt: str, span: int) -> int:
    digest = hashlib.sha256(f"{code}|{day}|{salt}".encode()).digest()
    return int.from_bytes(digest[:4], "big") % span


def _times(code: str, day: date, half: bool) -> tuple[time, time]:
    """In and out, local time. Inside grace (in by 09:44, out from 18:31)."""
    if half:
        in_at = datetime.combine(day, time(9, 10)) + timedelta(minutes=_jitter(code, day, "i", 30))
        out_at = datetime.combine(day, time(14, 50)) + timedelta(
            minutes=_jitter(code, day, "o", 20)
        )
    else:
        in_at = datetime.combine(day, time(9, 5)) + timedelta(minutes=_jitter(code, day, "i", 40))
        out_at = datetime.combine(day, time(18, 31)) + timedelta(
            minutes=_jitter(code, day, "o", 45)
        )
    return in_at.time(), out_at.time()


async def _reload(session: AsyncSession, *rows: object) -> None:
    """A rollback expires every loaded row; read them back before going on."""
    for row in rows:
        await session.refresh(row)


async def _get_or_create_type(session, actor, code, **fields) -> LeaveType:
    found = await session.scalar(
        tenant_scoped(select(LeaveType).where(LeaveType.code == code), LeaveType, actor)
    )
    if found is not None:
        return found
    return await leave.create_type(session, actor, LeaveTypeCreate(code=code, **fields))


async def main(args: argparse.Namespace) -> int:
    url = LOCAL_URL if args.db == "local" else settings.DATABASE_URL
    if args.db == "live" and not args.apply:
        print("Live database: add --apply to write. Nothing done.")
        return 1
    engine = create_async_engine(url, poolclass=NullPool)
    log: list[str] = []

    def say(line: str) -> None:
        print(line)
        log.append(line)

    async with AsyncSession(engine, expire_on_commit=False) as session:
        tenant = await session.scalar(select(Tenant).where(Tenant.slug == WORKSPACE))
        assert tenant is not None, "no CGB workspace"
        actor = await session.scalar(
            select(User)
            .join(Employee, Employee.id == User.employee_id)
            .where(
                User.tenant_id == tenant.id,
                User.role == UserRole.ADMIN,
                Employee.employee_code == "EMP0001",
            )
        )
        assert actor is not None, "no admin login to act as"
        say(f"Workspace {tenant.name}; acting as admin {actor.full_name} ({args.db} database)")
        if await attendance.active_lock_covering(session, actor, START, END) is not None:
            say("September attendance is locked: unlock it before seeding. Nothing done.")
            return 1

        # 1. Work week ---------------------------------------------------------
        entity = (
            await session.scalars(tenant_scoped(select(LegalEntity), LegalEntity, actor))
        ).one()
        general = await session.scalar(
            tenant_scoped(select(Shift).where(Shift.code == "GEN"), Shift, actor)
        )
        assert general is not None and list(general.weekly_off_days) == [6, 7]
        if entity.default_shift_id != general.id:
            await __import__("app.services.org", fromlist=["x"]).update_legal_entity(
                session, actor, entity.id, LegalEntityUpdate(default_shift_id=general.id),
                request=None,
            )
            say(f"Work week of {entity.name}: General shift, Mon-Fri, Sat & Sun off")
        else:
            say(f"Work week of {entity.name} already the General shift")

        # 2. Leave setup -------------------------------------------------------
        cl = await _get_or_create_type(
            session, actor, "CL", name="Casual Leave", is_paid=True, allows_half_day=True,
            description="Paid leave. Needs balance.",
        )
        lwp = await _get_or_create_type(
            session, actor, "LWP", name="Leave Without Pay", is_paid=False, allows_half_day=True,
            description="Unpaid leave: the days are loss of pay in payroll.",
        )
        policy = await session.scalar(
            tenant_scoped(
                select(LeavePolicy).where(
                    LeavePolicy.leave_type_id == cl.id, LeavePolicy.is_active.is_(True)
                ),
                LeavePolicy,
                actor,
            )
        )
        if policy is None:
            await leave.create_policy(
                session,
                actor,
                LeavePolicyCreate(
                    leave_type_id=cl.id,
                    name="Casual Leave",
                    annual_quota_days=Decimal(0),
                    accrual_frequency="annual",
                    effective_from=date(2026, 1, 1),
                ),
            )
            say("Leave: Casual Leave (paid) with a policy, Leave Without Pay (unpaid)")

        employees = (
            await session.scalars(
                tenant_scoped(
                    select(Employee)
                    .where(
                        Employee.legal_entity_id == entity.id,
                        Employee.deleted_at.is_(None),
                        Employee.status.in_(
                            [
                                EmploymentStatus.ACTIVE,
                                EmploymentStatus.ON_NOTICE,
                                EmploymentStatus.ON_LEAVE,
                            ]
                        ),
                        Employee.date_of_joining <= END,
                    )
                    .order_by(Employee.employee_code),
                    Employee,
                    actor,
                )
            )
        ).all()
        employees = [e for e in employees if e.date_of_exit is None or e.date_of_exit >= START]
        by_code = {e.employee_code: e for e in employees}
        missing = sorted(set(PLANS) - set(by_code))
        assert not missing, f"plan names people not employed in September: {missing}"
        say(f"{len(employees)} people employed in September 2026")

        for code, plan in PLANS.items():
            if plan.cl_balance is None:
                continue
            try:
                await ledger.adjust(
                    session,
                    actor,
                    BalanceAdjustmentCreate(
                        employee_id=by_code[code].id,
                        leave_type_id=cl.id,
                        year=YEAR,
                        kind="opening",
                        days=plan.cl_balance,
                        reason=REASON,
                    ),
                )
            except HTTPException as exc:
                if exc.status_code != 409:
                    raise
                await session.rollback()
                await _reload(session, actor, cl, lwp, *employees)

        # 3. Punches -----------------------------------------------------------
        zones = await attendance.employee_timezones(session, actor, [e.id for e in employees])
        rows: list[attendance.ImportRow] = []
        for employee in employees:
            plan = PLANS.get(employee.employee_code, Plan("Present every weekday"))
            full_leave = {
                day
                for lv in plan.leaves
                if lv.half is None
                for day in range(lv.start, lv.end + 1)
            }
            half_leave = {lv.start for lv in plan.leaves if lv.half is not None}
            first = max(START, employee.date_of_joining or START)
            day = first
            while day <= END:
                if day.isoweekday() <= 5 and day.day not in plan.absent and (
                    day.day not in full_leave
                ):
                    half = day.day in plan.half or day.day in half_leave
                    in_t, out_t = _times(employee.employee_code, day, half)
                    zone = zones[employee.id]
                    for direction, at in ((PunchDirection.IN, in_t), (PunchDirection.OUT, out_t)):
                        when = datetime.combine(day, at, tzinfo=zone).astimezone(UTC)
                        rows.append(
                            attendance.ImportRow(
                                row=len(rows) + 1,
                                employee_code=employee.employee_code,
                                punched_at=when,
                                direction=direction,
                                device_id=DEVICE,
                                raw={"seed": SEED, "local": f"{day} {at:%H:%M}"},
                            )
                        )
                day += timedelta(days=1)
        result = await attendance.import_punch_rows(session, actor, rows)
        say(
            f"Punches: {result['imported']} imported, {result['duplicates']} already there, "
            f"{len(result['errors'])} problems"
        )

        # 4. Leave requests ----------------------------------------------------
        types = {"CL": cl, "LWP": lwp}
        refusals: dict[str, str] = {}
        for code, plan in PLANS.items():
            employee = by_code[code]
            for lv in plan.leaves:
                leave_type = types[lv.type]
                exists = await session.scalar(
                    tenant_scoped(
                        select(LeaveRequest.id).where(
                            LeaveRequest.employee_id == employee.id,
                            LeaveRequest.leave_type_id == leave_type.id,
                            LeaveRequest.start_date == d(lv.start),
                            LeaveRequest.end_date == d(lv.end),
                            LeaveRequest.reason == REASON,
                            LeaveRequest.status != LeaveRequestStatus.CANCELLED,
                        ),
                        LeaveRequest,
                        actor,
                    )
                )
                if exists is not None:
                    continue
                if lv.try_cl_first:
                    try:
                        await leave.apply(
                            session,
                            actor,
                            LeaveRequestCreate(
                                employee_id=employee.id,
                                leave_type_id=cl.id,
                                start_date=d(lv.start),
                                end_date=d(lv.end),
                                reason=REASON,
                            ),
                        )
                        raise SystemExit(f"{code}: Casual Leave without balance was accepted!")
                    except HTTPException as exc:
                        await session.rollback()
                        await _reload(session, actor, cl, lwp, *employees)
                        refusals[code] = str(exc.detail)
                        say(f"{code}: Casual Leave refused as expected - {exc.detail}")
                made = await leave.apply(
                    session,
                    actor,
                    LeaveRequestCreate(
                        employee_id=employee.id,
                        leave_type_id=leave_type.id,
                        start_date=d(lv.start),
                        end_date=d(lv.end),
                        is_half_day=lv.half is not None,
                        half_day_session=lv.half,
                        reason=REASON,
                    ),
                )
                if lv.outcome == "approved":
                    for _ in range(6):
                        row = await session.get(LeaveRequest, made.id)
                        await session.refresh(row)
                        if row.status is not LeaveRequestStatus.PENDING:
                            break
                        await leave.decide(
                            session, actor, made.id, approve=True, note="Test data: approved"
                        )
                elif lv.outcome == "rejected":
                    await leave.decide(
                        session, actor, made.id, approve=False, note="Test data: not approved"
                    )
                row = await session.get(LeaveRequest, made.id)
                await session.refresh(row)
                say(
                    f"{code}: {leave_type.name} {d(lv.start):%d}-{d(lv.end):%d %b}"
                    f"{' (' + lv.half.replace('_', ' ') + ')' if lv.half else ''}"
                    f" {row.total_days} day(s) -> {row.status.value}"
                )
                assert row.status.value == lv.outcome or (
                    lv.outcome == "rejected" and row.status.value == "rejected"
                ), f"{code}: wanted {lv.outcome}, got {row.status.value}"

        # 5. Recompute September ----------------------------------------------
        written = await attendance.recompute_range(
            session, actor, employee_ids=[e.id for e in employees], start=START, end=END
        )
        say(f"Recomputed {written} attendance day(s) for 1-30 Sep")

        # Report ---------------------------------------------------------------
        report = []
        for employee in employees:
            days = (
                await session.scalars(
                    tenant_scoped(
                        select(AttendanceDay).where(
                            AttendanceDay.employee_id == employee.id,
                            AttendanceDay.attendance_date.between(START, END),
                        ),
                        AttendanceDay,
                        actor,
                    )
                )
            ).all()
            count: dict[str, int] = {}
            for row in days:
                count[row.status.value] = count.get(row.status.value, 0) + 1
            lop = await attendance.get_lop_days(
                session, actor, employee.id, START, END, require_lock=False
            )
            lines = (await leave.balance_lines(session, actor, [employee], YEAR)).get(
                employee.id, []
            )
            cl_left = next(
                (str(b.balance.available) for b in lines if b.leave_type.id == cl.id), ""
            )
            plan = PLANS.get(employee.employee_code, Plan("Present every weekday"))
            report.append(
                {
                    "code": employee.employee_code,
                    "name": employee.full_name,
                    "scenario": plan.what,
                    "days": len(days),
                    "present": count.get("present", 0),
                    "half_day": count.get("half_day", 0),
                    "on_leave": count.get("on_leave", 0),
                    "lop_status": count.get("lop", 0),
                    "absent": count.get("absent", 0),
                    "week_off": count.get("week_off", 0),
                    "other": {
                        k: v
                        for k, v in count.items()
                        if k not in {"present", "half_day", "on_leave", "lop", "absent", "week_off"}
                    }
                    or "",
                    "lop_days": str(lop),
                    "cl_left": cl_left,
                }
            )
        path = OUT / f"september_seed_{args.db}.csv"
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(report[0]))
            writer.writeheader()
            writer.writerows(report)
        for r in report:
            print(
                f"{r['code']:<11} {r['name'][:22]:<22} P{r['present']:>3} H{r['half_day']:>2} "
                f"L{r['on_leave']:>2} LOP{r['lop_status']:>2} A{r['absent']:>2} "
                f"WO{r['week_off']:>2} {r['other']}  lop={r['lop_days']:<5} cl={r['cl_left']:<5}"
                f" {r['scenario']}"
            )
        (OUT / f"september_seed_{args.db}.log").write_text("\n".join(log), encoding="utf-8")
    await engine.dispose()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", choices=["local", "live"], required=True)
    parser.add_argument("--apply", action="store_true")
    raise SystemExit(asyncio.run(main(parser.parse_args())))
