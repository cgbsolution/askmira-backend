"""Clear a workspace's people before a fresh import; keep its admin and settings.

    cd backend
    # What would go — nothing is saved:
    .venv/Scripts/python.exe scripts/reset_workspace_people.py --workspace <slug>
    # Do it:
    .venv/Scripts/python.exe scripts/reset_workspace_people.py --workspace <slug> --apply

Kept: the admin the platform created with the workspace (its first admin
login; `--keep-email` picks another admin), that admin's employee record, and
every setting. Removed: every other login and employee record, and all
punches, attendance, leave, salaries, payroll runs, payslips, documents and
notifications — the kept admin's own included. The audit trail stays. Take a
database backup first: this cannot be undone. See
`app/services/workspace_people_reset.py`.

The database is the one in DATABASE_URL; the host is printed before anything
runs. Files are deleted through this machine's storage settings, after the
commit: when they point elsewhere than the server's, the keys are listed.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.services import workspace_people_reset as reset
from app.services.storage.cleanup import delete_files


async def main(args: argparse.Namespace) -> int:
    url = make_url(settings.DATABASE_URL)
    print(f"Database: {url.host}:{url.port}/{url.database}")
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            tenant = (
                await session.execute(
                    text("select id, name from tenants where slug = :s and deleted_at is null"),
                    {"s": args.workspace},
                )
            ).one_or_none()
            if tenant is None:
                print(f"No workspace {args.workspace}.")
                return 1
            if args.keep_email:
                keep = await session.scalar(
                    text("select id from users where tenant_id = :t and lower(email) = lower(:e)"),
                    {"t": tenant.id, "e": args.keep_email},
                )
            else:
                keep = await session.scalar(
                    text(
                        "select id from users where tenant_id = :t and role::text = 'ADMIN' "
                        "order by created_at limit 1"
                    ),
                    {"t": tenant.id},
                )
            if keep is None:
                print("No admin login to keep.")
                return 1

            try:
                report = await reset.reset_people(session, tenant.id, keep_user_id=keep)
            except reset.ResetError as exc:
                await session.rollback()
                print(exc)
                return 1

            print(f"\n{tenant.name}: keep {report.kept_email} ({report.kept_code or 'no record'})")
            print("\nRemoved:")
            for name, n in sorted(report.removed.items()):
                print(f"  {name:<36} {n}")
            print("\nKept (settings, audit trail):")
            for name, n in sorted(report.kept.items()):
                print(f"  {name:<36} {n}")
            if report.links_cleared:
                print("\nLinks to removed people emptied on kept rows:")
                for name, n in sorted(report.links_cleared.items()):
                    print(f"  {name:<36} {n}")
            if report.mentions_left:
                print("\nKept rows still mentioning a removed person in their data:")
                for name, n in sorted(report.mentions_left.items()):
                    print(f"  {name:<36} {n}")
            print(f"\nUploaded files of removed rows: {len(report.files)}")
            for key in sorted(report.files):
                print(f"  {key}")

            if not args.apply:
                await session.rollback()
                print("\nDry run: nothing saved. Add --apply to do it.")
                return 0
            await session.commit()
            print("\nSaved.")
    finally:
        await engine.dispose()

    if report.files:
        print(f"Deleting files through STORAGE_BACKEND={settings.STORAGE_BACKEND}:")
        print(" ", await delete_files(report.files))
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workspace", required=True, help="workspace address (slug)")
    parser.add_argument("--keep-email", help="admin login to keep (default: the first admin)")
    parser.add_argument("--apply", action="store_true", help="save; without it, a dry run")
    sys.exit(asyncio.run(main(parser.parse_args())))
