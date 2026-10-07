"""Fill the demo company with every HRMS flow, driven through the real API.

    cd backend
    .venv/Scripts/python.exe scripts/seed_flows.py                    # seed what is not done yet
    .venv/Scripts/python.exe scripts/seed_flows.py --reset-passwords  # and print fresh logins

First runs `seed_demo.py` (the company and its role logins), then each flow in
`demo_flows/` as the people who would do it in the portal: employees apply,
managers approve, HR locks attendance, one admin computes payroll and another
approves it. A failing step stops the seed and names the request, the person
and the server's reason — the seed is also an end-to-end check of the product.

Writes to whatever DATABASE_URL points at, and prints the host and database
name first. Each finished flow is recorded on the demo tenant, so re-running
continues where the last run stopped instead of duplicating anything.

Dates are relative to today: attendance and payroll cover the last three
complete months, leave and approvals sit around today.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx
import seed_demo
from demo_flows import (
    assets,
    attendance,
    compliance,
    culture,
    documents,
    exits,
    expenses,
    helpdesk,
    hiring,
    onboarding,
    organisation,
    payroll,
    people,
    performance,
    time_off,
)
from demo_flows.api import Api, Demo, Person, SeedError
from demo_flows.cast import CAST
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.db.session import SessionLocal
from app.main import app
from app.models.tenant import Tenant
from app.models.user import User

FLOWS = [
    ("people", people.run),
    ("leave setup", time_off.setup),
    ("asset register", assets.setup),
    ("exit checklist", exits.setup),
    ("leave history", time_off.history),
    ("ravi exit", exits.ravi),
    ("attendance", attendance.run),
    ("payroll setup", payroll.setup),
    ("payroll runs", payroll.runs),
    ("reimbursement claims", payroll.claims),
    ("statutory filings", payroll.filings),
    ("exit benefits", payroll.benefits),
    ("documents setup", documents.setup),
    ("ravi settlement", exits.settlement),
    ("documents", documents.current),
    ("leave today", time_off.current),
    ("asset requests", assets.current),
    ("neha resigns", exits.neha),
    ("expense setup", expenses.setup),
    ("expenses", expenses.run),
    ("performance setup", performance.setup),
    ("annual review", performance.annual),
    ("mid-year review", performance.midyear),
    ("conversations", performance.conversations),
    ("development", performance.development),
    ("hiring setup", hiring.setup),
    ("hiring", hiring.run),
    ("onboarding setup", onboarding.setup),
    ("onboarding", onboarding.run),
    ("helpdesk setup", helpdesk.setup),
    ("helpdesk", helpdesk.run),
    ("culture feed", culture.feed),
    ("awards", culture.awards),
    ("surveys", culture.survey),
    ("announcements", culture.announcements),
    ("rewards", culture.rewards),
    ("organisation", organisation.run),
    ("compliance", compliance.run),
]


async def _load_state(tenant_slug: str) -> tuple[uuid.UUID, dict]:
    async with SessionLocal() as session:
        tenant = await session.scalar(select(Tenant).where(Tenant.slug == tenant_slug))
        if tenant is None:
            raise SystemExit("The demo tenant does not exist; seed_demo should have made it.")
        return tenant.id, dict((tenant.settings or {}).get("demo_seed") or {})


async def _save_state(tenant_id: uuid.UUID, state: dict) -> None:
    async with SessionLocal() as session:
        tenant = await session.get(Tenant, tenant_id)
        tenant.settings = {**(tenant.settings or {}), "demo_seed": state}
        flag_modified(tenant, "settings")
        await session.commit()


async def _attach_people(demo: Demo, http: httpx.AsyncClient, state: dict) -> None:
    async with SessionLocal() as session:
        users = {
            u.email: u
            for u in await session.scalars(select(User).where(User.tenant_id == demo.tenant_id))
        }
    known = state.get("people", {})
    for person in demo.people.values():
        user = users.get(person.email)
        if user is not None:
            person.user_id = user.id
            person.employee_id = user.employee_id
            person.api = Api(http, user.id, demo.tenant_id, user.role.value, person.name)
        elif person.key in known and known[person.key].get("employee_id"):
            person.employee_id = uuid.UUID(known[person.key]["employee_id"])
    # Sana is hired during the seed, so she is not in the cast.
    sana = users.get("sana@askmira-demo.com")
    if sana is not None and "sana" not in demo.people:
        demo.people["sana"] = Person(
            key="sana", code="DEMO023", first_name="Sana", last_name="Iqbal",
            email=sana.email, department="ENG", designation="Software Engineer",
            grade="P3", manager="vikram", date_of_joining="2026-10-19",
            employee_id=sana.employee_id, user_id=sana.id,
            api=Api(http, sana.id, demo.tenant_id, sana.role.value, "Sana Iqbal"),
        )


async def main(reset_passwords: bool, only: str | None, stop_after: str | None) -> None:
    print(f"Writing the demo company's flows to {seed_demo.describe_target()}")
    base = await seed_demo.seed(reset_passwords)
    passwords = {person.email: password for person, password in base if password}

    tenant_id, state = await _load_state(seed_demo.TENANT_SLUG)
    done: list[str] = list(state.get("done", []))
    demo = Demo(http=None, tenant_id=tenant_id, people={p.key: p for p in CAST})  # type: ignore[arg-type]
    demo.ids = dict(state.get("ids", {}))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://demo", timeout=300) as http:
        demo.http = http
        await _attach_people(demo, http, state)
        for name, flow in FLOWS:
            if name in done and name != only:
                print(f"- {name}: already seeded")
                continue
            if only and name != only:
                continue
            print(f"- {name}")
            started = time.monotonic()
            try:
                await flow(demo)
            except SeedError as exc:
                print(f"\nFLOW '{name}' FAILED\n  {exc}\n")
                raise SystemExit(1) from exc
            done.append(name) if name not in done else None
            state = {
                "done": done,
                "ids": demo.ids,
                "people": {
                    p.key: {"employee_id": str(p.employee_id) if p.employee_id else None}
                    for p in demo.people.values()
                },
            }
            await _save_state(tenant_id, state)
            print(f"  ({time.monotonic() - started:.1f}s)")
            if name == stop_after:
                break

    for person in demo.people.values():
        if person.password:
            passwords[person.email] = person.password
    print()
    if not passwords:
        print("Logins unchanged. Re-run with --reset-passwords for a fresh set.")
        return
    print(f"{'NAME':<18} {'ROLE':<12} {'LOGIN EMAIL':<34} PASSWORD")
    for person in seed_demo.PEOPLE:
        if person.employee_code is None and person.email in passwords:
            print(f"{person.full_name:<18} {person.role.value:<12} {person.email:<34} "
                  f"{passwords[person.email]}")
    for person in demo.people.values():
        if person.email in passwords:
            print(f"{person.name:<18} {person.role:<12} {person.email:<34} "
                  f"{passwords[person.email]}")
    print()
    print("Passwords are shown once and stored only as hashes.")


def cli() -> None:
    # Server messages carry arrows and rupee signs; a Windows console would choke.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--reset-passwords", action="store_true",
                        help="New passwords for the role logins seed_demo creates.")
    parser.add_argument("--only", help="Run (or re-run) just this flow.")
    parser.add_argument("--stop-after", help="Stop once this flow has run.")
    args = parser.parse_args()
    asyncio.run(main(args.reset_passwords, args.only, args.stop_after))


if __name__ == "__main__":
    cli()
