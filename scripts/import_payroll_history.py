"""Load payroll history from factoHR salary registers (one Excel file a month).

    cd backend
    # Check everything; nothing is saved (the default):
    .venv/Scripts/python.exe scripts/import_payroll_history.py --workspace <slug> --dir <folder>
    # Save it:
    .venv/Scripts/python.exe scripts/import_payroll_history.py --workspace <slug> \
        --dir <folder> --apply
    # Keep the report (JSON) and the issues workbook to work through:
    ... --report out.json --issues out.xlsx

Each month becomes one locked, published payroll run marked "imported", with one
payslip per person exactly as factoHR paid it, and each person's salary
revisions are dated from the registers. Nothing is saved while the check finds
any error; a month that already has a run is refused, never duplicated. The dry
run writes everything inside a transaction and rolls it back, so the database's
own checks run too. See `app/services/payroll/history_import.py`.

The database is the one in DATABASE_URL; its host is printed before anything
runs. The import acts as the workspace's first admin login.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.models.user import User
from app.services.payroll import history_import, history_parse


def _print_report(report: dict) -> None:
    print("\nMonth      rows  slips  gross            net paid         register bank   diff")
    for m in report["months"]:
        diffs = m["differences"]
        bank = diffs.get("bank_transfer_rounded") or diffs.get("bank_transfer") or {}
        print(
            f"{m['label']:<9} {m['rows_read']:>5} {m['payslips']:>6}  "
            f"{m['totals']['gross']:>15}  {m['totals']['net']:>15}  "
            f"{bank.get('in_register', '-'):>15}  {bank.get('difference', '-'):>8}"
            + ("  REFUSED" if m["refused"] else "")
        )
    print(f"\nPayslips: {report['payslips']}")
    print("Payslips with a line for:", report["line_counts"])
    salary = report["salary"]
    print(
        f"Salary revisions: {salary['revisions_new']} new, {salary['revisions_redated']} re-dated, "
        f"{salary['revisions_deleted']} replaced; increments with an unknown date: "
        f"{len(salary['increments_date_unknown'])}; no salary in AskMira: "
        f"{len(salary['no_live_salary'])}; left as they are: {len(salary['left_as_is'])}"
    )
    print("Salary actions:", salary["actions"])
    people = report["people"]
    print(
        f"People: {people['matched']} by code, {len(people['mapped_by_name'])} by name and "
        f"joining date, {len(people['unmatched'])} not found"
    )
    for level in ("error", "warning", "note"):
        counts = report["issue_counts"].get(level, {})
        if counts:
            print(f"{level}s:", dict(sorted(counts.items())))
    for issue in report["issues"]:
        if issue["level"] == "error":
            print("  ERROR", issue["month"] or "", issue["code"] or "", issue["message"])


async def main(args: argparse.Namespace) -> int:
    url = make_url(settings.DATABASE_URL)
    print(f"Database: {url.host}:{url.port}/{url.database}")
    folder = Path(args.dir)
    paths = sorted(
        p
        for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() == ".xlsx" and not p.name.startswith("~$")
    )
    if not paths:
        print(f"No .xlsx files in {folder}.")
        return 1
    started = time.perf_counter()
    months = history_parse.parse_registers([(p.name, p.read_bytes()) for p in paths])
    parsed = time.perf_counter()
    print(f"Read {len(paths)} files in {parsed - started:.1f}s.")

    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    statements = {"n": 0}

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _count(*_args, **_kwargs) -> None:
        statements["n"] += 1

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
            admin_id = await session.scalar(
                text(
                    "select id from users where tenant_id = :t and role::text = 'ADMIN' "
                    "order by created_at limit 1"
                ),
                {"t": tenant.id},
            )
            if admin_id is None:
                print("No admin login in this workspace.")
                return 1
            entity_id = None
            if args.entity:
                entity_id = await session.scalar(
                    text("select id from legal_entities where tenant_id = :t and code = :c"),
                    {"t": tenant.id, "c": args.entity},
                )
                if entity_id is None:
                    print(f"No company with code {args.entity}.")
                    return 1
            user = await session.get(User, admin_id)
            assert user is not None
            print(f"{tenant.name}: acting as {user.email}")

            before = statements["n"]
            planned_at = time.perf_counter()
            result = await history_import.plan(session, user, months, legal_entity_id=entity_id)
            plan_statements = statements["n"] - before
            plan_seconds = time.perf_counter() - planned_at

            before = statements["n"]
            applied_at = time.perf_counter()
            if not result.blocking:
                result = await history_import.apply(session, user, result, dry_run=not args.apply)
            else:
                await session.rollback()
            apply_statements = statements["n"] - before
            apply_seconds = time.perf_counter() - applied_at
    finally:
        await engine.dispose()

    report = history_import.preview(result)
    report["stats"].update(
        {
            "files": len(paths),
            "parse_seconds": round(parsed - started, 1),
            "plan_seconds": round(plan_seconds, 1),
            "plan_statements": plan_statements,
            "apply_seconds": round(apply_seconds, 1),
            "apply_statements": apply_statements,
        }
    )
    _print_report(report)
    print(
        f"\nTiming: read {report['stats']['parse_seconds']}s, check {plan_seconds:.1f}s "
        f"({plan_statements} statements), write {apply_seconds:.1f}s ({apply_statements} "
        "statements)."
    )
    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
        print(f"Report: {args.report}")
    if args.issues:
        Path(args.issues).write_bytes(history_import.issues_xlsx(result))
        print(f"Issues workbook: {args.issues}")

    if result.blocking:
        print(f"\n{result.blocking} error(s): nothing saved. Fix them and run the check again.")
        return 2
    if not args.apply:
        print("\nDry run: everything was written and rolled back; nothing saved. Add --apply.")
        return 0
    print(f"\nSaved: {len(result.run_ids)} months.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workspace", required=True, help="workspace address (slug)")
    parser.add_argument("--dir", required=True, help="folder holding the register .xlsx files")
    parser.add_argument("--entity", help="company code, when the workspace has several")
    parser.add_argument("--apply", action="store_true", help="save; without it, a dry run")
    parser.add_argument("--report", help="write the full report here (JSON)")
    parser.add_argument("--issues", help="write the issues workbook here (.xlsx)")
    sys.exit(asyncio.run(main(parser.parse_args())))
