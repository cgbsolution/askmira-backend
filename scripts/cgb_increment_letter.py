"""CGB's increment letter as the workspace's increment letter template.

Asked for on 2026-11-25: the increment letter in CGB's own format ("Increment
Letter - <name>.pdf"): fixed CTC and variable pay stated apart, the "1. Fixed
CTC" annexure and the "2. Variable Plan" table, every value from the record.
The wording is `INCREMENT_CGB_SPEC` in app/services/documents/default_templates.py.

Saved the way HR saves an edited template (Documents -> Templates), as the next
version of INCREMENT_STD, audited as the acting admin (`--as`, else the
workspace's first admin). Earlier versions stay, so letters already issued
still verify; HR can go back by saving an older version's text again.

    cd backend
    # What would change, nothing saved:
    .venv/Scripts/python.exe scripts/cgb_increment_letter.py --workspace cgb-solutions
    # Do it:
    ... --apply

A rerun when the active version already has this text changes nothing. Needs
the code that knows `{{#if}}`, `salary.fixed_ctc_annual` and
`computed.fixed_ctc_annexure` deployed first, or the letters will not issue.
The database is the one in DATABASE_URL; its host is printed before anything runs.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.models.documents import LetterTemplate
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services.documents import service as letters
from app.services.documents.default_templates import INCREMENT_CGB_SPEC


async def main(args: argparse.Namespace) -> int:
    url = make_url(settings.DATABASE_URL)
    print(f"Database: {url.host}:{url.port}/{url.database}")
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
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
                print("No active admin to act as. Pass --as.")
                return 1
            print(f"Workspace: {tenant.name} ({tenant.slug}); acting as {actor.email}")

            code = INCREMENT_CGB_SPEC["code"]
            versions = list(
                (
                    await session.scalars(
                        select(LetterTemplate)
                        .where(LetterTemplate.tenant_id == tenant.id, LetterTemplate.code == code)
                        .order_by(LetterTemplate.version)
                    )
                ).all()
            )
            print(f"\n{code} versions now:")
            for v in versions:
                print(f"  v{v.version} {'active ' if v.active else 'retired'} {v.name}")
            if not versions:
                print("  (none: this will be version 1)")
            active = next((v for v in versions if v.active), None)
            body = INCREMENT_CGB_SPEC["body_html"].strip()
            if active is not None and active.body_html.strip() == body:
                print("\nThe active version already has CGB's wording. Nothing to do.")
                return 0

            if not args.apply:
                print(
                    f"\nWould save v{(versions[-1].version + 1) if versions else 1}: "
                    f"{INCREMENT_CGB_SPEC['name']}."
                )
                print("Dry run: nothing saved. Add --apply to do it.")
                return 0

            saved = await letters.create_template_version(
                session,
                actor,
                code=code,
                payload={
                    "name": INCREMENT_CGB_SPEC["name"],
                    "category": INCREMENT_CGB_SPEC["category"],
                    "body_html": body,
                    "header_html": None,
                    "footer_html": None,
                    # Released increments were already approved; keep what the
                    # workspace had chosen, else none.
                    "requires_approval": bool(active and active.requires_approval),
                    "approver_roles": list(active.approver_roles) if active else [],
                    "requires_signature": bool(active and active.requires_signature),
                    "signatory_role": active.signatory_role if active else None,
                    "use_letterhead": True,
                },
                fields=INCREMENT_CGB_SPEC["fields"],
            )
            print(f"\nSaved {code} v{saved.version}: {saved.name}.")
            return 0
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--as", dest="as_email")
    parser.add_argument("--apply", action="store_true")
    sys.exit(asyncio.run(main(parser.parse_args())))
