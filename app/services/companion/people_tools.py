"""Ask Mira's lookups about people: one person's record, and the company's headcount.

Two audiences, two rules:

- An admin or sub-admin may ask about anybody in their workspace ("what is
  Deepak's salary"). The name is searched; when more than one person matches,
  the answer is a list to pick from (name, employee code, title, department),
  and the pick asks again by the person's id. A sub-admin needs the right to
  view the area the answer comes from (payroll for salary, time for leave and
  attendance, people for the profile), and a sub-admin narrowed to some
  departments or locations only ever finds their people (hr_scope narrows
  every ORM query in the request).
- Everybody else gets their own record only, by the employee id in their
  sign-in, whatever name they type. Asking about someone else is refused
  before anything is read.

Nothing here is ever given to a model: the cards go to the screen.
"""

from __future__ import annotations

import re
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, distinct, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import access_control, hr_scope, permissions
from app.core.deps import tenant_scoped
from app.models.employee import Employee, EmploymentStatus
from app.models.org import Department, Location
from app.models.payroll import ComponentType, EmployeeSalary
from app.models.user import User, UserRole
from app.services.documents.indian_numbers import format_inr

__all__ = [
    "MAX_CHOICES",
    "OWN_LOOKUPS",
    "PERSON_LOOKUPS",
    "find_people",
    "headcount",
    "may_view",
    "own_record",
    "person_record",
]

#: What an admin may look up about one person, and the area a sub-admin needs to view.
PERSON_LOOKUPS: dict[str, str] = {
    "salary": "payroll",
    "leave_balance": "time",
    "attendance_this_month": "time",
    "manager": "people",
    "profile": "people",
}

#: Lookups about the asker's own record answered here (the rest by assistant).
OWN_LOOKUPS = ("my_salary", "my_profile")

#: The most people listed to pick from.
MAX_CHOICES = 10

_EMPLOYED = (EmploymentStatus.ACTIVE, EmploymentStatus.ON_NOTICE, EmploymentStatus.ON_LEAVE)

_STATUS_LABEL = {
    EmploymentStatus.ACTIVE: "Active",
    EmploymentStatus.ON_NOTICE: "On notice",
    EmploymentStatus.ON_LEAVE: "On long leave",
    EmploymentStatus.PRE_JOINING: "Yet to join",
    EmploymentStatus.EXITED: "Exited",
}

_LOOKUP_LABEL = {
    "salary": "Salary",
    "leave_balance": "Leave balance",
    "attendance_this_month": "Attendance this month",
    "manager": "Manager",
    "profile": "Profile",
}


# ── Access ───────────────────────────────────────────────────────────────────


async def may_view(session: AsyncSession, user: User, area: str) -> bool:
    """Whether this person may read `area` about other people.

    Admins may. A sub-admin may unless their rights in that area (their own,
    else their role's) take viewing away. Employees and managers may not: Ask
    Mira answers them about themselves only.
    """
    if user.role is UserRole.SUPER_ADMIN or permissions.is_admin(user):
        return True
    if user.role is not UserRole.SUB_ADMIN or not permissions.is_hr(user):
        return False
    rights = await access_control.rights_for(session, user, area)
    return rights is None or rights.can_view


# ── Cards ────────────────────────────────────────────────────────────────────


def _notice(text: str, tone: str = "info") -> dict[str, Any]:
    return {"type": "notice", "tone": tone, "text": text}


def _record(
    title: str,
    *,
    subtitle: str = "",
    heading: str = "",
    figures: list[tuple[str, str]] | None = None,
    columns: list[str] | None = None,
    rows: list[list[Any]] | None = None,
    note: str = "",
    open_href: str = "",
    open_label: str = "",
) -> dict[str, Any]:
    card: dict[str, Any] = {
        "type": "record",
        "title": title,
        "subtitle": subtitle,
        "heading": heading,
        "figures": [{"label": label, "value": value} for label, value in figures or []],
        "note": note,
    }
    if columns and rows:
        card["table"] = {"columns": columns, "rows": rows}
    if open_href:
        card["open"] = open_href
        card["open_label"] = open_label or "Open the page"
    return card


def _days(value: Decimal | float | int) -> str:
    number = Decimal(str(value)).normalize()
    return f"{number:f}"


def _day(value: date | None) -> str:
    return f"{value:%d %b %Y}" if value else "—"


async def _names(
    session: AsyncSession, user: User, employees: list[Employee]
) -> tuple[dict[uuid.UUID, str], dict[uuid.UUID, str]]:
    """Department and location names for these people."""
    dept_ids = {e.department_id for e in employees if e.department_id}
    loc_ids = {e.location_id for e in employees if e.location_id}
    departments: dict[uuid.UUID, str] = {}
    locations: dict[uuid.UUID, str] = {}
    if dept_ids:
        for row in await session.execute(
            tenant_scoped(
                select(Department.id, Department.name).where(Department.id.in_(dept_ids)),
                Department,
                user,
            )
        ):
            departments[row.id] = row.name
    if loc_ids:
        for row in await session.execute(
            tenant_scoped(
                select(Location.id, Location.name).where(Location.id.in_(loc_ids)), Location, user
            )
        ):
            locations[row.id] = row.name
    return departments, locations


def _subtitle(employee: Employee, departments: dict[uuid.UUID, str]) -> str:
    parts = [
        employee.employee_code,
        employee.designation or "",
        departments.get(employee.department_id, "") if employee.department_id else "",
    ]
    if employee.status is not EmploymentStatus.ACTIVE:
        parts.append(_STATUS_LABEL.get(employee.status, employee.status.value))
    return " · ".join(p for p in parts if p)


# ── Finding people ───────────────────────────────────────────────────────────


def _words(query: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9.@+'/-]+", query.lower()) if w.strip(".-'")][:4]


def _matches(word: str):
    # Typed text is matched literally: a "%" or "_" in it is not a wildcard.
    literal = word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{literal}%"
    # An email is matched by the part before the "@" unless the whole address is
    # typed: every work email shares the company's domain, so "ra" would match
    # everyone at askmira.com.
    email = (
        Employee.work_email.ilike(pattern, escape="\\")
        if "@" in word
        else func.split_part(Employee.work_email, "@", 1).ilike(pattern, escape="\\")
    )
    return or_(
        Employee.first_name.ilike(pattern, escape="\\"),
        Employee.last_name.ilike(pattern, escape="\\"),
        Employee.display_name.ilike(pattern, escape="\\"),
        Employee.employee_code.ilike(pattern, escape="\\"),
        email,
    )


async def find_people(
    session: AsyncSession, user: User, query: str, *, limit: int = MAX_CHOICES
) -> tuple[list[Employee], int]:
    """People matching a name, employee code or email: (the first `limit`, how many in all).

    An exact employee code wins outright. Every word must match (so "deepak
    rawat" and "rawat deepak" both find him); when nothing does, a trailing
    "s" is dropped once ("deepaks salary").
    """
    base = select(Employee).where(Employee.deleted_at.is_(None))
    code = query.strip().strip("()#").strip()
    if code:
        exact = (
            await session.scalars(
                tenant_scoped(
                    base.where(func.upper(Employee.employee_code) == code.upper()), Employee, user
                )
            )
        ).all()
        if exact:
            return list(exact), len(exact)

    words = _words(query)
    if not words:
        return [], 0
    tries = [words]
    trimmed = [w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words]
    if trimmed != words:
        tries.append(trimmed)
    for attempt in tries:
        condition = and_(*[_matches(w) for w in attempt])
        total = int(
            await session.scalar(
                tenant_scoped(
                    select(func.count(Employee.id)).where(Employee.deleted_at.is_(None), condition),
                    Employee,
                    user,
                )
            )
            or 0
        )
        if not total:
            continue
        rows = (
            await session.scalars(
                tenant_scoped(
                    base.where(condition).order_by(
                        # The people working here first; leavers last.
                        (Employee.status == EmploymentStatus.EXITED).asc(),
                        Employee.first_name,
                        Employee.employee_code,
                    ),
                    Employee,
                    user,
                ).limit(limit)
            )
        ).all()
        return list(rows), total
    return [], 0


async def _one(session: AsyncSession, user: User, employee_id: str) -> Employee | None:
    try:
        key = uuid.UUID(str(employee_id))
    except ValueError:
        return None
    return await session.scalar(
        tenant_scoped(
            select(Employee).where(Employee.id == key, Employee.deleted_at.is_(None)),
            Employee,
            user,
        )
    )


def _choices(
    people: list[Employee],
    total: int,
    departments: dict[uuid.UUID, str],
    *,
    query: str,
    lookup: str,
) -> dict[str, Any]:
    label = _LOOKUP_LABEL[lookup].lower()
    more = (
        f" Showing the first {len(people)} of {total}; type more of the name or the code."
        if total > len(people)
        else ""
    )
    return {
        "type": "choices",
        "title": f"{total} people match “{query}”",
        "caption": f"Pick whose {label} you want.{more}",
        "options": [
            {
                "label": person.full_name,
                "detail": _subtitle(person, departments),
                "text": f"{_LOOKUP_LABEL[lookup]} of {person.full_name} ({person.employee_code})",
                "tool": "employee_record",
                "inputs": {"employee_id": str(person.id), "person_lookup": lookup},
            }
            for person in people
        ],
    }


# ── One person's record ──────────────────────────────────────────────────────


async def _salary_in_force(
    session: AsyncSession, user: User, employee_id: uuid.UUID, today: date
) -> tuple[EmployeeSalary | None, EmployeeSalary | None]:
    """(the salary in force today, the next one saved for a later date)."""
    rows = (
        await session.scalars(
            tenant_scoped(
                select(EmployeeSalary)
                .where(EmployeeSalary.employee_id == employee_id)
                .order_by(EmployeeSalary.effective_from.desc()),
                EmployeeSalary,
                user,
            )
        )
    ).all()
    current = next(
        (
            s
            for s in rows
            if s.effective_from <= today and (s.effective_to is None or s.effective_to >= today)
        ),
        None,
    )
    upcoming = [s for s in rows if s.effective_from > today]
    return current, (upcoming[-1] if upcoming else None)


def _money(value: object) -> Decimal:
    return Decimal(str(value or 0))


async def _salary_card(
    session: AsyncSession,
    user: User,
    employee: Employee,
    *,
    today: date,
    own: bool,
    departments: dict[uuid.UUID, str],
) -> dict[str, Any]:
    from app.services.payroll.service import to_period_amounts

    href = "/pay" if own else f"/payroll/salaries?employee={employee.id}"
    label = "Open My pay" if own else "Open in Salary master"
    title = "Your salary" if own else employee.full_name
    subtitle = _subtitle(employee, departments)
    current, upcoming = await _salary_in_force(session, user, employee.id, today)
    later = ""
    if upcoming is not None:
        later = (
            f"A revision from {_day(upcoming.effective_from)} is saved: "
            f"annual CTC {format_inr(_money(upcoming.ctc_annual))}."
        )
    if current is None:
        none = (
            "No salary is in force for you today."
            if own
            else "No salary is in force today. Set one up in the Salary master."
        )
        return _record(
            title,
            subtitle=subtitle,
            heading="Current salary",
            note=" ".join(p for p in (none, later) if p),
            open_href=href,
            open_label=label,
        )

    components = sorted(current.components, key=lambda c: (c.display_order, c.component_code))
    earnings = [c for c in components if ComponentType(c.component_type) is ComponentType.EARNING]
    if current.gross_amount is not None:
        gross_annual, gross_monthly = to_period_amounts(
            _money(current.gross_amount), current.gross_basis or "monthly"
        )
    else:
        gross_monthly = sum((_money(c.monthly_amount) for c in earnings), Decimal(0))
        gross_annual = sum((_money(c.annual_amount) for c in earnings), Decimal(0))
    ctc_annual = _money(current.ctc_annual)

    kinds = {
        ComponentType.EARNING: "Earning",
        ComponentType.DEDUCTION: "Deduction",
        ComponentType.EMPLOYER_CONTRIBUTION: "Company contribution",
        ComponentType.REIMBURSEMENT: "Reimbursement",
    }
    rows = [
        [
            c.component_name,
            kinds.get(ComponentType(c.component_type), str(c.component_type)),
            format_inr(_money(c.monthly_amount)),
            format_inr(_money(c.annual_amount)),
        ]
        for c in components
        if _money(c.monthly_amount) or _money(c.annual_amount)
    ]
    structure = current.structure.name if current.structure else ""
    note = " · ".join(
        p
        for p in (
            f"In force from {_day(current.effective_from)}",
            f"Structure: {structure}" if structure else "",
            current.revision_reason or "",
        )
        if p
    )
    return _record(
        title,
        subtitle=subtitle,
        heading="Current salary",
        figures=[
            ("Monthly gross", format_inr(gross_monthly)),
            ("Annual gross", format_inr(gross_annual)),
            ("Monthly CTC", format_inr(ctc_annual / 12)),
            ("Annual CTC", format_inr(ctc_annual)),
        ],
        columns=["Wage type", "Kind", "Monthly", "Annual"],
        rows=rows,
        note=" ".join(p for p in (note + ".", later) if p),
        open_href=href,
        open_label=label,
    )


async def _profile_card(
    session: AsyncSession, user: User, employee: Employee, *, own: bool
) -> dict[str, Any]:
    departments, locations = await _names(session, user, [employee])
    manager = None
    if employee.manager_id:
        manager = await session.scalar(
            tenant_scoped(
                select(Employee).where(Employee.id == employee.manager_id), Employee, user
            )
        )
    return _record(
        "Your profile" if own else employee.full_name,
        subtitle=_subtitle(employee, departments),
        figures=[
            ("Employee code", employee.employee_code),
            ("Designation", employee.designation or "—"),
            (
                "Department",
                departments.get(employee.department_id, "—") if employee.department_id else "—",
            ),
            ("Location", locations.get(employee.location_id, "—") if employee.location_id else "—"),
            ("Reports to", manager.full_name if manager else "—"),
            ("Date of joining", _day(employee.date_of_joining)),
            ("Status", _STATUS_LABEL.get(employee.status, employee.status.value)),
            ("Work email", employee.work_email),
        ],
        open_href="/profile" if own else f"/people/{employee.id}",
        open_label="Open your profile" if own else "Open the profile",
    )


async def _leave_card(
    session: AsyncSession,
    user: User,
    employee: Employee,
    *,
    today: date,
    departments: dict[uuid.UUID, str],
) -> dict[str, Any]:
    from app.services.leave import service as leave_service

    balances = await leave_service.balances(session, user, employee_id=employee.id, year=today.year)
    rows = [
        [b.leave_type_name, _days(b.available), _days(b.used), _days(b.pending)]
        for b in balances
        if not b.is_statutory
    ]
    return _record(
        employee.full_name,
        subtitle=_subtitle(employee, departments),
        heading=f"Leave balance {today.year}",
        columns=["Leave type", "Available", "Taken", "Pending approval"],
        rows=rows,
        note="" if rows else "No leave balances are set up for this year.",
        open_href=f"/people/{employee.id}",
        open_label="Open the profile",
    )


async def _attendance_card(
    session: AsyncSession,
    user: User,
    employee: Employee,
    *,
    today: date,
    departments: dict[uuid.UUID, str],
) -> dict[str, Any]:
    from app.services.attendance import review

    period = await review.my_period(
        session, user, employee_id=employee.id, start=today.replace(day=1), end=today
    )
    summary = period.summary
    return _record(
        employee.full_name,
        subtitle=_subtitle(employee, departments),
        heading=f"Attendance {today.replace(day=1):%d %b} to {today:%d %b %Y}",
        figures=[
            ("Present", str(summary.present_days)),
            ("On leave", str(summary.on_leave_days)),
            ("Late arrivals", str(summary.late_count)),
            ("Loss-of-pay days", _days(summary.lop_days)),
        ],
        note=(
            f"{summary.needs_regularisation} day(s) need a correction."
            if summary.needs_regularisation
            else ""
        ),
        open_href="/time/attendance",
        open_label="Open attendance",
    )


async def person_record(
    session: AsyncSession, user: User, inputs: dict[str, Any], *, today: date
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """One person's salary, leave, attendance, manager or profile, for an admin or sub-admin."""
    lookup = str(inputs.get("person_lookup") or "none")
    if lookup not in PERSON_LOOKUPS:
        lookup = "profile"
    if not await may_view(session, user, PERSON_LOOKUPS[lookup]):
        return [
            _notice(
                "Your role cannot see that about other people. Ask about your own record "
                "instead, for example “my salary”.",
                "warning",
            )
        ], {}

    employee_id = str(inputs.get("employee_id") or "").strip()
    query = str(inputs.get("query") or "").strip()[:100]
    if employee_id:
        employee = await _one(session, user, employee_id)
        if employee is None:
            return [_notice("That person is not in your workspace, or not in your scope.")], {}
    else:
        if not query:
            return [
                _notice(
                    f"Whose {_LOOKUP_LABEL[lookup].lower()}? Type a name or an employee code, "
                    "for example “salary of Rohan” or “salary of EMP001”."
                )
            ], {}
        people, total = await find_people(session, user, query)
        if not people:
            return [_notice(f"Nobody in your workspace matches “{query}”.")], {
                "query": query,
                "person_lookup": lookup,
            }
        if total > 1:
            departments, _ = await _names(session, user, people)
            return [_choices(people, total, departments, query=query, lookup=lookup)], {
                "query": query,
                "person_lookup": lookup,
            }
        employee = people[0]

    used = {"employee_id": str(employee.id), "person_lookup": lookup}
    departments, _ = await _names(session, user, [employee])
    if lookup == "salary":
        card = await _salary_card(
            session, user, employee, today=today, own=False, departments=departments
        )
    elif lookup == "leave_balance":
        card = await _leave_card(session, user, employee, today=today, departments=departments)
    elif lookup == "attendance_this_month":
        card = await _attendance_card(session, user, employee, today=today, departments=departments)
    elif lookup == "manager":
        manager = None
        if employee.manager_id:
            manager = await session.scalar(
                tenant_scoped(
                    select(Employee).where(Employee.id == employee.manager_id), Employee, user
                )
            )
        card = _record(
            employee.full_name,
            subtitle=_subtitle(employee, departments),
            heading="Reports to",
            figures=[
                ("Manager", manager.full_name if manager else "No manager recorded"),
                ("Manager's title", (manager.designation or "—") if manager else "—"),
                ("Manager's code", manager.employee_code if manager else "—"),
            ],
            open_href=f"/people/{employee.id}",
            open_label="Open the profile",
        )
    else:
        card = await _profile_card(session, user, employee, own=False)
    return [card], used


async def own_record(
    session: AsyncSession, user: User, lookup: str, *, today: date
) -> list[dict[str, Any]]:
    """The asker's own salary or profile, by the employee id in their sign-in."""
    if user.employee_id is None:
        return [
            _notice(
                "Your sign-in is not linked to an employee record, so there is nothing of "
                "yours to look up."
            )
        ]
    employee = await _one(session, user, str(user.employee_id))
    if employee is None:
        return [_notice("Your employee record could not be found.")]
    if lookup == "my_salary":
        departments, _ = await _names(session, user, [employee])
        return [
            await _salary_card(
                session, user, employee, today=today, own=True, departments=departments
            )
        ]
    return [await _profile_card(session, user, employee, own=True)]


# ── The company's headcount ──────────────────────────────────────────────────


async def headcount(
    session: AsyncSession, user: User, *, today: date
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """How many people work here, how many are on payroll, and by department."""
    if not await may_view(session, user, "people"):
        return [_notice("Company figures are for admins and sub-admins.", "warning")], {}
    payroll = await may_view(session, user, "payroll")

    by_status: dict[EmploymentStatus, int] = {}
    for row in await session.execute(
        tenant_scoped(
            select(Employee.status, func.count(Employee.id))
            .where(Employee.deleted_at.is_(None))
            .group_by(Employee.status),
            Employee,
            user,
        )
    ):
        by_status[row[0]] = int(row[1])
    employed = sum(by_status.get(s, 0) for s in _EMPLOYED)

    in_force = and_(
        EmployeeSalary.effective_from <= today,
        or_(EmployeeSalary.effective_to.is_(None), EmployeeSalary.effective_to >= today),
    )
    paid_ids: set[uuid.UUID] = set()
    if payroll:
        paid_ids = set(
            (
                await session.scalars(
                    tenant_scoped(
                        tenant_scoped(
                            select(distinct(EmployeeSalary.employee_id))
                            .join(Employee, Employee.id == EmployeeSalary.employee_id)
                            .where(
                                in_force,
                                Employee.deleted_at.is_(None),
                                Employee.status.in_(_EMPLOYED),
                            ),
                            EmployeeSalary,
                            user,
                        ),
                        Employee,
                        user,
                    )
                )
            ).all()
        )

    figures = [
        ("Employed now", str(employed)),
        ("Active", str(by_status.get(EmploymentStatus.ACTIVE, 0))),
        ("On notice", str(by_status.get(EmploymentStatus.ON_NOTICE, 0))),
        ("On long leave", str(by_status.get(EmploymentStatus.ON_LEAVE, 0))),
        ("Yet to join", str(by_status.get(EmploymentStatus.PRE_JOINING, 0))),
        ("Exited", str(by_status.get(EmploymentStatus.EXITED, 0))),
    ]
    if payroll:
        figures[1:1] = [
            ("On payroll", str(len(paid_ids))),
            ("Employed, no salary set up", str(employed - len(paid_ids))),
        ]

    # By department: the people employed now.
    people = (
        await session.execute(
            tenant_scoped(
                select(Employee.id, Employee.department_id).where(
                    Employee.deleted_at.is_(None), Employee.status.in_(_EMPLOYED)
                ),
                Employee,
                user,
            )
        )
    ).all()
    dept_ids = {row.department_id for row in people if row.department_id}
    names: dict[uuid.UUID, str] = {}
    if dept_ids:
        for row in await session.execute(
            tenant_scoped(
                select(Department.id, Department.name).where(Department.id.in_(dept_ids)),
                Department,
                user,
            )
        ):
            names[row.id] = row.name
    tally: dict[str, list[int]] = {}
    for row in people:
        name = (
            names.get(row.department_id, "No department") if row.department_id else "No department"
        )
        counts = tally.setdefault(name, [0, 0])
        counts[0] += 1
        if row.id in paid_ids:
            counts[1] += 1
    ordered = sorted(tally.items(), key=lambda item: (-item[1][0], item[0]))
    columns = ["Department", "Employed"] + (["On payroll"] if payroll else [])
    rows = [[name, c[0], *([c[1]] if payroll else [])] for name, c in ordered]

    note = (
        f"As of {_day(today)}. Employed = active, on notice or on long leave. "
        + ("On payroll = employed with a salary in force today." if payroll else "")
    ).strip()
    return [
        _record(
            "Headcount",
            heading="Your departments and locations"
            if hr_scope.current_scope()
            else "Your workspace",
            figures=figures,
            columns=columns,
            rows=rows,
            note=note,
            open_href="/people",
            open_label="Open Employees",
        )
    ], {}
