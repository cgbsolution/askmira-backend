"""Take out the second address line where it only repeats the first.

factoHR's export copies the whole address into "Address 2" for some people.
Imports before the fix kept both, so their profiles show the address twice.
The import now drops the repeat; this cleans up people already imported.

    cd backend
    # Who would change — nothing is saved:
    .venv/Scripts/python.exe scripts/dedupe_address_lines.py --workspace <slug> --as <admin email>
    # Do it:
    .venv/Scripts/python.exe scripts/dedupe_address_lines.py --workspace <slug> \
        --as <admin email> --apply

Each person is saved through the same path as HR's Addresses → Edit, so each
change has its own audit row, in the name of the admin given with --as.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

import app.models  # noqa: F401  (every mapper, so relationships resolve)
from app.core.config import settings
from app.models.employee import Employee, EmployeeProfile
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.employee_profile import Addresses, AddressesWrite
from app.services import employee_profile
from app.services.employee_import.service import _lines


def _clean(address: dict | None) -> dict | None:
    """The address with a repeated line taken out, or None if nothing repeats."""
    if not address or not address.get("line1"):
        return None
    line1, line2 = _lines([address.get("line1"), address.get("line2")])
    if (line1, line2) == (address.get("line1"), address.get("line2") or None):
        return None
    return {**address, "line1": line1, "line2": line2}


async def main(args: argparse.Namespace) -> int:
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            tenant = await session.scalar(select(Tenant).where(Tenant.slug == args.workspace))
            if tenant is None:
                print(f"No workspace {args.workspace}.")
                return 1
            actor = await session.scalar(
                select(User).where(
                    User.tenant_id == tenant.id, func.lower(User.email) == args.actor.lower()
                )
            )
            if actor is None:
                print(f"No login {args.actor} in {tenant.name}.")
                return 1

            rows = (
                await session.execute(
                    select(EmployeeProfile, Employee)
                    .join(Employee, Employee.id == EmployeeProfile.employee_id)
                    .where(EmployeeProfile.tenant_id == tenant.id)
                    .order_by(Employee.employee_code)
                )
            ).all()

            fixes = []
            for profile, employee in rows:
                code, name = employee.employee_code, employee.full_name
                stored = profile.addresses or {}
                current = _clean(stored.get("current"))
                permanent = _clean(stored.get("permanent"))
                if current is None and permanent is None:
                    continue
                fixes.append((profile.employee_id, code, name, stored, current, permanent))
                print(f"{code:<14} {name}")
                for label, fixed in (("current", current), ("permanent", permanent)):
                    if fixed is not None:
                        before = stored[label].get("line2")
                        print(f"    {label}: line 2 {before!r} -> {fixed['line2']!r}")

            if not fixes:
                print(f"{tenant.name}: no address repeats its first line.")
                return 0
            if not args.apply:
                print(f"\n{len(fixes)} people would change. Add --apply to save.")
                return 0

            for employee_id, _code, _name, stored, current, permanent in fixes:
                data = Addresses(
                    current=current or stored.get("current"),
                    permanent=permanent or stored.get("permanent"),
                    permanent_same_as_current=bool(stored.get("permanent_same_as_current")),
                )
                await employee_profile.write_section(
                    session, actor, employee_id, AddressesWrite(section="addresses", data=data)
                )
            print(f"\nDone: {len(fixes)} people saved, each with an audit row.")
            return 0
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workspace", required=True, help="the workspace slug")
    parser.add_argument("--as", dest="actor", required=True, help="an admin's login email")
    parser.add_argument("--apply", action="store_true", help="save; without it nothing changes")
    sys.exit(asyncio.run(main(parser.parse_args())))
