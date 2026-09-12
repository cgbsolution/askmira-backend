"""Create a demo company with one login for every role.

    cd backend
    .venv/Scripts/python.exe scripts/seed_demo.py                    # create, print logins
    .venv/Scripts/python.exe scripts/seed_demo.py --reset-passwords  # new passwords

Writes to whatever DATABASE_URL points at, and prints the host and database name
(never the credentials) before it writes, so it is obvious which one that is.

Passwords are generated at run time and printed once. They are not stored
anywhere except as Argon2id hashes, so a lost password means re-running with
`--reset-passwords` - there is nothing to look up.

Idempotent: re-running reuses the demo company, its org structure, its employees
and its users rather than creating duplicates.

Why a script and not signup: signup only ever creates a `tenant_admin`, and SCIM
and SSO only ever create an `employee`. Nothing in the product creates the other
four roles yet, so without this there is no way to sign in as them.
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
import string
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select
from sqlalchemy.engine import make_url

from app.core.config import settings
from app.core.security import hash_password
from app.db.session import SessionLocal
from app.models.audit import AuditAction
from app.models.employee import Employee, EmploymentStatus
from app.models.org import Department, LegalEntity, Location
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.schemas.auth import LoginRequest
from app.services import audit

TENANT_SLUG = "askmira-demo"
TENANT_NAME = "AskMira Demo"
DOMAIN = "askmira-demo.com"

#: Every module switched on, so each role can be checked across the whole
#: product. Signup's defaults leave analytics, benefits and rewards off.
ALL_MODULES = {
    "core_hr": True,
    "leave": True,
    "payroll": True,
    "culture": True,
    "hiring": True,
    "analytics": True,
    "benefits": True,
    "rewards": True,
}


@dataclass(frozen=True)
class Person:
    role: UserRole
    email: str
    full_name: str
    #: None for platform_admin, which is AskMira staff and has no employee record.
    employee_code: str | None
    department: str | None
    designation: str | None
    grade: str | None
    #: The employee_code of this person's manager.
    reports_to: str | None


PEOPLE = (
    Person(UserRole.PLATFORM_ADMIN, f"platform@{DOMAIN}", "Platform Admin",
           None, None, None, None, None),
    Person(UserRole.TENANT_ADMIN, f"admin@{DOMAIN}", "Ananya Rao",
           "DEMO001", "ENG", "Chief Executive Officer", "E1", None),
    Person(UserRole.HR_ADMIN, f"hr.admin@{DOMAIN}", "Priya Ramesh",
           "DEMO002", "HR", "Head of People", "M4", "DEMO001"),
    Person(UserRole.HR_OPS, f"hr.ops@{DOMAIN}", "Karthik Iyer",
           "DEMO003", "HR", "HR Operations Executive", "P2", "DEMO002"),
    Person(UserRole.MANAGER, f"manager@{DOMAIN}", "Vikram Shah",
           "DEMO004", "ENG", "Engineering Manager", "M3", "DEMO001"),
    Person(UserRole.EMPLOYEE, f"employee@{DOMAIN}", "Meera Nair",
           "DEMO005", "ENG", "Software Engineer", "P3", "DEMO004"),
)


def generate_password(length: int = 16) -> str:
    """Letters and digits, with the look-alikes removed so it can be typed.

    Always at least one letter and one digit, which is what the password policy
    requires.
    """
    alphabet = "".join(
        c for c in string.ascii_letters + string.digits if c not in "0O1lI"
    )
    while True:
        candidate = "".join(secrets.choice(alphabet) for _ in range(length))
        if any(c.isalpha() for c in candidate) and any(c.isdigit() for c in candidate):
            return candidate


def describe_target() -> str:
    url = make_url(settings.DATABASE_URL)
    return f"{url.host}:{url.port}/{url.database}"


async def get_or_create_tenant(session) -> Tenant:
    tenant = await session.scalar(select(Tenant).where(Tenant.slug == TENANT_SLUG))
    if tenant is None:
        tenant = Tenant(
            name=TENANT_NAME,
            slug=TENANT_SLUG,
            enabled_modules=dict(ALL_MODULES),
            settings={
                "fiscal_year_start_month": 4,
                "week_start": "monday",
                "currency": "INR",
            },
        )
        session.add(tenant)
        await session.flush()
    else:
        tenant.enabled_modules = dict(ALL_MODULES)
        tenant.is_active = True
    return tenant


async def get_or_create_org(session, tenant: Tenant):
    entity = await session.scalar(
        select(LegalEntity).where(
            LegalEntity.tenant_id == tenant.id, LegalEntity.code == "DEMO"
        )
    )
    if entity is None:
        entity = LegalEntity(
            tenant_id=tenant.id, name="AskMira Demo Pvt Ltd", code="DEMO"
        )
        session.add(entity)
        await session.flush()

    location = await session.scalar(
        select(Location).where(Location.tenant_id == tenant.id, Location.code == "BLR")
    )
    if location is None:
        location = Location(
            tenant_id=tenant.id,
            legal_entity_id=entity.id,
            name="Bengaluru",
            code="BLR",
            state_code="KA",
        )
        session.add(location)
        await session.flush()

    departments = {}
    for code, name in (("ENG", "Engineering"), ("HR", "People")):
        department = await session.scalar(
            select(Department).where(
                Department.tenant_id == tenant.id, Department.code == code
            )
        )
        if department is None:
            department = Department(tenant_id=tenant.id, name=name, code=code)
            session.add(department)
            await session.flush()
        departments[code] = department

    return entity, location, departments


async def seed(reset_passwords: bool) -> list[tuple[Person, str | None]]:
    # Refuse before writing anything if the login form would reject an address.
    for person in PEOPLE:
        LoginRequest(email=person.email, password="Validation1")

    async with SessionLocal() as session:
        tenant = await get_or_create_tenant(session)
        entity, location, departments = await get_or_create_org(session, tenant)

        employees: dict[str, Employee] = {}
        for person in PEOPLE:
            if person.employee_code is None:
                continue
            employee = await session.scalar(
                select(Employee).where(
                    Employee.tenant_id == tenant.id,
                    Employee.employee_code == person.employee_code,
                )
            )
            first, _, last = person.full_name.partition(" ")
            if employee is None:
                employee = Employee(
                    tenant_id=tenant.id,
                    employee_code=person.employee_code,
                    first_name=first,
                    last_name=last or None,
                    work_email=person.email,
                    legal_entity_id=entity.id,
                    location_id=location.id,
                    department_id=departments[person.department].id,
                    designation=person.designation,
                    grade=person.grade,
                    status=EmploymentStatus.ACTIVE,
                    date_of_joining=date(2024, 4, 1),
                )
                session.add(employee)
                await session.flush()
            employees[person.employee_code] = employee

        for person in PEOPLE:
            if person.employee_code and person.reports_to:
                employees[person.employee_code].manager_id = employees[person.reports_to].id

        results: list[tuple[Person, str | None]] = []
        created: list[str] = []
        for person in PEOPLE:
            user = await session.scalar(
                select(User).where(User.email == person.email)
            )
            password: str | None = None
            if user is None:
                password = generate_password()
                user = User(
                    tenant_id=tenant.id,
                    email=person.email,
                    password_hash=hash_password(password),
                    full_name=person.full_name,
                    role=person.role,
                    is_active=True,
                    is_email_verified=True,
                )
                session.add(user)
                created.append(person.email)
            else:
                if user.tenant_id != tenant.id:
                    raise SystemExit(
                        f"{person.email} already exists in another company. "
                        "Refusing to move it into the demo company."
                    )
                user.role = person.role
                user.is_active = True
                if reset_passwords:
                    password = generate_password()
                    user.password_hash = hash_password(password)
            if person.employee_code:
                user.employee_id = employees[person.employee_code].id
            await session.flush()
            results.append((person, password))

        await audit.record(
            session,
            tenant_id=tenant.id,
            action=AuditAction.CREATE,
            entity_type="tenant",
            entity_id=tenant.id,
            actor_type="system",
            summary=(
                f"Demo seed: {len(created)} login(s) created"
                + (", passwords reset" if reset_passwords else "")
                + "."
            ),
            changes={"created": created, "passwords_reset": reset_passwords},
        )
        await session.commit()
        return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--reset-passwords",
        action="store_true",
        help="Generate new passwords for demo users that already exist.",
    )
    args = parser.parse_args()

    print(f"Writing demo company '{TENANT_NAME}' to {describe_target()}")
    results = asyncio.run(seed(args.reset_passwords))

    print()
    print(f"{'ROLE':<16} {'LOGIN EMAIL':<30} PASSWORD")
    for person, password in results:
        shown = password or "(unchanged - re-run with --reset-passwords)"
        print(f"{person.role.value:<16} {person.email:<30} {shown}")
    print()
    print("Passwords are shown once and stored only as hashes.")


if __name__ == "__main__":
    main()
