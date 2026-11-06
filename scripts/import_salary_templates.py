"""Import salary structure templates into a workspace (Payroll → Salary structures).

    cd backend
    # Check them and show a split for an example CTC — nothing is saved:
    .venv/Scripts/python.exe scripts/import_salary_templates.py --workspace <slug> \
        --file scripts/salary_templates/ctc_calculator_50_basic_pf25000.json
    # Save them:
    ... --apply
    # Save, replacing the rules of structures with the same names:
    ... --apply --replace

The file holds `templates`: each one exactly what the Salary structures editor
saves (name, rounding, a rule per wage type, employer PF / ESIC inside the CTC).
They go through the same service as the editor, so they are checked the same
way and audited as the acting admin (`--as`, else the workspace's first admin).
A structure whose name is taken is left as it is unless `--replace`.

The database is the one in DATABASE_URL; its host is printed before anything runs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.models.payroll import SalaryStructure
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.schemas.payroll import SalaryTemplateInput
from app.services.payroll import templates


async def main(args: argparse.Namespace) -> int:
    url = make_url(settings.DATABASE_URL)
    print(f"Database: {url.host}:{url.port}/{url.database}")
    data = json.loads(Path(args.file).read_text(encoding="utf-8"))
    payloads = [SalaryTemplateInput.model_validate(item) for item in data["templates"]]
    print(f"File: {args.file} — {len(payloads)} template(s)")

    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            tenant = await session.scalar(
                select(Tenant).where(Tenant.slug == args.workspace, Tenant.deleted_at.is_(None))
            )
            if tenant is None:
                print(f"No workspace {args.workspace}.")
                return 1
            actor_stmt = select(User).where(User.tenant_id == tenant.id, User.is_active.is_(True))
            if args.as_email:
                actor_stmt = actor_stmt.where(func.lower(User.email) == args.as_email.lower())
            else:
                actor_stmt = actor_stmt.where(User.role == UserRole.ADMIN).order_by(User.created_at)
            actor = (await session.scalars(actor_stmt.limit(1))).first()
            if actor is None or actor.role not in (UserRole.ADMIN, UserRole.SUB_ADMIN):
                print("No admin to act as. Pass --as with an admin's email.")
                return 1
            print(f"Workspace: {tenant.name} ({tenant.slug}); acting as {actor.email}")

            failed = 0
            for payload in payloads:
                print(f"\n— {payload.name}")
                try:
                    split = await templates.preview_draft(
                        session, actor, payload, Decimal(args.ctc), "monthly"
                    )
                except HTTPException as exc:
                    print(f"  Cannot be imported: {exc.detail}")
                    failed += 1
                    continue
                print(f"  Example, CTC {args.ctc} a month:")
                for line in split.lines:
                    if line.monthly or not line.applied:
                        state = "" if line.applied else f"  ({line.note})"
                        name = line.component_name
                        print(f"    {name:<34} {line.monthly:>12}  {line.rule_text}{state}")
                for row in split.employer:
                    state = "" if row.applied else f"  ({row.note})"
                    name = f"{row.name} (in CTC)"
                    print(f"    {name:<34} {row.monthly:>12}  {row.rule_text}{state}")
                print(f"    {'MONTHLY GROSS':<34} {split.gross_monthly:>12}")

                existing = await session.scalar(
                    select(SalaryStructure).where(
                        SalaryStructure.tenant_id == tenant.id,
                        func.lower(SalaryStructure.name) == payload.name.lower(),
                    )
                )
                if not args.apply:
                    print(
                        "  Would "
                        + (
                            ("replace the rules of" if args.replace else "leave as it is")
                            + " the existing structure"
                            if existing
                            else "create it"
                        )
                    )
                    continue
                if existing and not args.replace:
                    print("  A structure with this name exists: left as it is (use --replace).")
                    continue
                if existing:
                    saved = await templates.update_template(session, actor, existing.id, payload)
                    print(f"  Replaced the rules of {saved.name} ({saved.id})")
                else:
                    saved = await templates.create_template(session, actor, payload)
                    print(f"  Created {saved.name} ({saved.id})")
            if not args.apply:
                print("\nNothing saved. Run again with --apply to import.")
            return 1 if failed else 0
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--workspace", required=True, help="The workspace's slug")
    parser.add_argument("--file", required=True, help="A JSON file of templates")
    parser.add_argument("--as", dest="as_email", help="Admin email to act (and be audited) as")
    parser.add_argument(
        "--ctc", default="25000", help="Example monthly CTC to show (default 25000)"
    )
    parser.add_argument("--apply", action="store_true", help="Save (default: check only)")
    parser.add_argument("--replace", action="store_true", help="Replace same-named structures")
    sys.exit(asyncio.run(main(parser.parse_args())))
