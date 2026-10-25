"""Fold a duplicate employee record into the real one.

    cd backend
    # Who has two records in the workspace:
    .venv/Scripts/python.exe scripts/merge_duplicate_employee.py --workspace <slug>
    # What a merge would move — nothing is saved:
    .venv/Scripts/python.exe scripts/merge_duplicate_employee.py --workspace <slug> \\
        --keep CGB-007 --drop EMP0001
    # Do it:
    .venv/Scripts/python.exe scripts/merge_duplicate_employee.py --workspace <slug> \\
        --keep CGB-007 --drop EMP0001 --apply

Keep the record with the person's data (the one imported from the old HR
system); drop the bare one the workspace made for its admin. The login, and
anything else pointing at the dropped record, moves to the kept one. See
`app/services/employee_merge.py`.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.models.tenant import Tenant
from app.models.user import User
from app.services import employee_merge


async def main(args: argparse.Namespace) -> int:
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            tenant = await session.scalar(select(Tenant).where(Tenant.slug == args.workspace))
            if tenant is None:
                print(f"No workspace {args.workspace}.")
                return 1

            if not args.keep:
                groups = await employee_merge.duplicates(session, tenant.id)
                if not groups:
                    print(f"{tenant.name}: nobody has two records.")
                for group in groups:
                    print(f"\n{group[0].work_email}")
                    for employee in group:
                        login = await session.scalar(
                            select(User.email).where(User.employee_id == employee.id)
                        )
                        print(
                            f"  {employee.employee_code:<14} {employee.full_name:<28} "
                            f"joined {employee.date_of_joining}  "
                            f"added {employee.created_at:%d %b %Y}"
                            f"  {'login: ' + login if login else 'no login'}"
                        )
                return 0

            try:
                report = await employee_merge.merge(
                    session, tenant.id, keep_code=args.keep, drop_code=args.drop
                )
            except employee_merge.MergeError as exc:
                await session.rollback()
                print(exc)
                return 1
            print(f"Keep {report.keep_code}, drop {report.drop_code}. Moved:")
            for name, count in report.moved.items():
                print(f"  {name}: {count}")
            if not report.moved:
                print("  nothing pointed at it")
            if args.apply:
                await session.commit()
                print(f"Done: {report.drop_code} is gone and everything is on {report.keep_code}.")
            else:
                await session.rollback()
                print("Nothing saved. Run again with --apply to do it.")
            return 0
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--workspace", required=True, help="the workspace's slug")
    parser.add_argument("--keep", help="code of the record to keep (the one with the data)")
    parser.add_argument("--drop", help="code of the record to fold in and delete")
    parser.add_argument("--apply", action="store_true", help="save; without it nothing is")
    parsed = parser.parse_args()
    if bool(parsed.keep) != bool(parsed.drop):
        parser.error("--keep and --drop go together")
    sys.exit(asyncio.run(main(parsed)))
