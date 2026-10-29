"""Statutory rule sets for CGB Solutions: PF, ESI and income tax (TDS), as drafts.

    cd C:/cgbcode/askmira/backend
    .venv/Scripts/python.exe scripts/cgb_september_test/rule_sets_cgb.py --db local --approve pf15
    .venv/Scripts/python.exe scripts/cgb_september_test/rule_sets_cgb.py --db live --apply

Entered as the workspace admin through the rule-set service, unapproved. On the
live database nothing is approved here: approval is the second admin's
attestation that the figures match the cited source, made in Payroll setup ->
Rule sets. `--approve` exists for the local copy only, to test a run.

Professional Tax is not entered: every salaried person has a PT_MANUAL amount on
the salary master, and a manual amount replaces what a PT rule set would give.
No state's Labour Welfare Fund falls in September; none is entered.
Re-running skips a version already entered (same type, place and start date).
The Rs 25,000 PF ceiling (from 17 Sep 2026) is entered only with --versions ...,pf25.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import seed_sep  # noqa: E402  (notify off, identifiers never written back)
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.models.employee import Employee  # noqa: E402
from app.models.payroll import PayrollRuleSet  # noqa: E402
from app.models.tenant import Tenant  # noqa: E402
from app.models.user import User, UserRole  # noqa: E402
from app.schemas.payroll_run import RuleSetCreate  # noqa: E402
from app.services.payroll import rule_sets  # noqa: E402

VERSIONS: dict[str, RuleSetCreate] = {
    "pf15": RuleSetCreate(
        rule_type="pf",
        jurisdiction="IN",
        effective_from=date(2014, 9, 1),
        rules={
            "wage_ceiling": 15000,
            "employee_rate": 12,
            "employer_rate": 12,
            "eps_wage_ceiling": 15000,
            "edli_wage_ceiling": 15000,
            "eps_rate": 8.33,
            "apply_wage_ceiling": True,
            "contribution_rounding": "nearest_rupee",
        },
        source_reference=(
            "EPF & MP Act 1952 s.6 (12% + 12%); EPS 1995 para 3 (8.33%); wage ceiling "
            "Rs 15,000 per G.S.R. 609(E) of 22 Aug 2014, w.e.f. 1 Sep 2014"
        ),
        notes=(
            "Contributions on Basic up to Rs 15,000, as the CGB CTCs show (employer PF "
            "Rs 1,800 + Rs 150 admin/EDLI a month). Superseded on 17 Sep 2026 by the "
            "Rs 25,000 ceiling (S.O. 5109(E)) once that version is approved."
        ),
    ),
    "pf25": RuleSetCreate(
        rule_type="pf",
        jurisdiction="IN",
        effective_from=date(2026, 9, 17),
        rules={
            "wage_ceiling": 25000,
            "employee_rate": 12,
            "employer_rate": 12,
            "eps_wage_ceiling": 25000,
            "edli_wage_ceiling": 25000,
            "eps_rate": 8.33,
            "apply_wage_ceiling": True,
            "contribution_rounding": "nearest_rupee",
        },
        source_reference=(
            "Ministry of Labour & Employment S.O. 5109(E), wage ceiling Rs 25,000 for "
            "Chapter III of the Code on Social Security 2020 (EPF), w.e.f. 17 Sep 2026"
        ),
        notes=(
            "CHECK BEFORE APPROVING: (1) the EPS and EDLI ceilings here follow secondary "
            "sources (8.33% x 25,000 = Rs 2,083 EPS); confirm against the S.O. text. "
            "(2) Payroll applies the version in force on a run's last day to the whole "
            "month: approved, September is computed on Rs 25,000 for all 30 days, not "
            "split at the 17th. Guidance differs (split at 17 Sep vs from October)."
        ),
    ),
    "esi": RuleSetCreate(
        rule_type="esi",
        jurisdiction="IN",
        effective_from=date(2019, 7, 1),
        rules={
            "wage_threshold": 21000,
            "employee_rate": "0.75",
            "employer_rate": "3.25",
            "contribution_rounding": "next_rupee",
            "contribution_periods": [
                {"start_month": 4, "end_month": 9, "label": "APR-SEP"},
                {"start_month": 10, "end_month": 3, "label": "OCT-MAR"},
            ],
        },
        source_reference=(
            "ESI rates 0.75% / 3.25% per G.S.R. 423(E) of 13 Jun 2019 w.e.f. 1 Jul 2019; "
            "wage limit Rs 21,000 w.e.f. 1 Jan 2017; unchanged under the Code on Social "
            "Security 2020 as of Oct 2026"
        ),
        notes="Contributions rounded up to the next rupee, as ESIC collects them.",
    ),
    "tds": RuleSetCreate(
        rule_type="tds",
        jurisdiction="IN",
        effective_from=date(2026, 4, 1),
        rules={
            "financial_year_start_month": 4,
            "cess_percent": 4,
            "tds_rounding": "nearest_rupee",
            "surcharge_bands": [
                {"above": 5000000, "up_to": 10000000, "rate": 10},
                {"above": 10000000, "up_to": 20000000, "rate": 15},
                {"above": 20000000, "up_to": None, "rate": 25},
            ],
            "regimes": {
                "new": {
                    "slabs": [
                        {"up_to": 400000, "rate": 0},
                        {"up_to": 800000, "rate": 5},
                        {"up_to": 1200000, "rate": 10},
                        {"up_to": 1600000, "rate": 15},
                        {"up_to": 2000000, "rate": 20},
                        {"up_to": 2400000, "rate": 25},
                        {"up_to": None, "rate": 30},
                    ],
                    "standard_deduction": 75000,
                    "rebate": {
                        "income_limit": 1200000,
                        "max_rebate": 60000,
                        "marginal_relief": True,
                    },
                    "deductions_allowed": ["80CCD2"],
                }
            },
        },
        source_reference=(
            "Income-tax Act 2025 (from 1 Apr 2026) with Finance Act 2026: new-regime "
            "slabs unchanged from Finance Act 2025; standard deduction Rs 75,000; "
            "rebate (old s.87A) up to Rs 60,000 for income up to Rs 12 lakh; cess 4%; "
            "new-regime surcharge capped at 25%"
        ),
        notes=(
            "New regime only: nobody has chosen the old regime. Someone who does needs "
            "an old-regime version first (slabs, HRA, section limits)."
        ),
    ),
}


async def main(args: argparse.Namespace) -> int:
    url = seed_sep.LOCAL_URL if args.db == "local" else settings.DATABASE_URL
    if args.db == "live" and not args.apply:
        print("Live database: add --apply to enter the drafts. Nothing done.")
        return 1
    approve = [a for a in (args.approve or "").split(",") if a]
    if approve and args.db == "live":
        print("Approval on the live database is the second admin's, in the portal.")
        return 1
    engine = create_async_engine(url, poolclass=NullPool)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        tenant = await session.scalar(select(Tenant).where(Tenant.slug == seed_sep.WORKSPACE))
        maker = await session.scalar(
            select(User)
            .join(Employee, Employee.id == User.employee_id)
            .where(
                User.tenant_id == tenant.id,
                User.role == UserRole.ADMIN,
                Employee.employee_code == "EMP0001",
            )
        )
        made: dict[str, object] = {}
        for key in [k for k in args.versions.split(",") if k]:
            payload = VERSIONS[key]
            existing = await session.scalar(
                select(PayrollRuleSet).where(
                    PayrollRuleSet.tenant_id == tenant.id,
                    PayrollRuleSet.rule_type == payload.rule_type,
                    PayrollRuleSet.jurisdiction == payload.jurisdiction,
                    PayrollRuleSet.effective_from == payload.effective_from,
                )
            )
            if existing is not None:
                made[key] = existing.id
                print(f"{key}: already entered ({'approved' if existing.approved_at else 'draft'})")
                continue
            row = await rule_sets.create_rule_set(session, maker, payload)
            made[key] = row.id
            print(f"{key}: entered as a draft, effective {payload.effective_from}")
        if approve:
            checker = await session.scalar(
                select(User).where(
                    User.tenant_id == tenant.id, User.role == UserRole.ADMIN, User.id != maker.id
                )
            )
            for key in approve:
                await rule_sets.approve_rule_set(session, checker, made[key], attest=True)
                print(f"{key}: approved (local copy only)")
    await engine.dispose()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", choices=["local", "live"], required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--approve", help="local copy only: comma list, e.g. pf15,esi,tds")
    # pf25 only when the company decides: an unapproved version in force on a
    # run's last day stops that run, and approved it prices all of September at 25,000.
    parser.add_argument("--versions", default="pf15,esi,tds")
    raise SystemExit(asyncio.run(main(parser.parse_args())))
