"""The payroll journal: an approved run, posted to the company's own ledgers.

What the journal is, in accounting terms:

- **Debits** — the expense side: every earning (salary components, overtime,
  incentives, reimbursements) and the employer's own contributions (PF, ESI,
  LWF, employer NPS).
- **Credits** — where the money is owed: each deduction to its liability or
  recovery ledger (PF payable, ESI payable, PT payable, TDS payable, loan
  recoveries), each employer contribution to its liability, and net pay to
  salary payable.

It always balances, because net pay *is* earnings less deductions; a journal
that does not balance means a payslip that does not add up, and the export
refuses rather than produce one.

Every line code posts to the ledger the tenant mapped it to. An unmapped code
stops the export with the list of what is missing — a ledger name the books do
not have is either rejected by Tally or Zoho, or silently created as a stray
ledger, and neither is an acceptable outcome for a payroll close.

Three formats, all from the same lines:

- **CSV** — a generic journal any system can import.
- **Tally XML** — a Journal voucher in Tally's import envelope. Debits are
  negative amounts with ISDEEMEDPOSITIVE=Yes, credits positive with No, which
  is how Tally represents them. Departments become cost-centre allocations.
- **Zoho Books** — the manual-journal import layout (one row per line; Zoho's
  import screen maps the columns).
"""

from __future__ import annotations

import csv
import io
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from xml.sax.saxutils import escape

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.accounting import LedgerMapping, LedgerNature
from app.models.audit import AuditAction
from app.models.employee import Employee
from app.models.org import Department, LegalEntity
from app.models.payroll import PayrollRun, PayrollRunStatus, Payslip
from app.models.user import User
from app.services import audit

__all__ = [
    "JournalFormat",
    "JournalLine",
    "build_journal",
    "export_journal",
    "mapping_status",
    "save_mappings",
    "serialise",
]

ZERO = Decimal(0)
CENT = Decimal("0.01")

#: Input-line kinds and how they post. A perquisite moves no cash and posts nothing.
_INPUT_NATURE = {
    "earning": LedgerNature.EARNING,
    "reimbursement": LedgerNature.EARNING,
    "deduction": LedgerNature.DEDUCTION,
    "recovery": LedgerNature.DEDUCTION,
    "employer_contribution": LedgerNature.EMPLOYER_CONTRIBUTION,
}
_LINE_NATURE = {
    "earning": LedgerNature.EARNING,
    "reimbursement": LedgerNature.EARNING,
    "deduction": LedgerNature.DEDUCTION,
    "employer_contribution": LedgerNature.EMPLOYER_CONTRIBUTION,
}

NET_PAY = "NET_PAY"
TDS = "TDS"


class JournalFormat:
    CSV = "csv"
    TALLY = "tally"
    ZOHO = "zoho"
    ALL = ("csv", "tally", "zoho")


@dataclass(frozen=True, slots=True)
class PostingLine:
    """One amount from one payslip, before it is mapped to a ledger."""

    line_code: str
    name: str
    nature: LedgerNature
    amount: Decimal
    cost_centre: str | None


@dataclass(slots=True)
class JournalLine:
    ledger: str
    debit: Decimal
    credit: Decimal
    cost_centre: str | None
    line_codes: set[str]


def _d(value: object) -> Decimal:
    return Decimal(str(value)) if value is not None else ZERO


def postings_for(payslip: Payslip, cost_centre: str | None) -> list[PostingLine]:
    """Every amount on one payslip, with the nature that decides its ledger side."""
    out: list[PostingLine] = []
    for line in payslip.line_items or []:
        nature = _LINE_NATURE.get(str(line.get("type", "")))
        amount = _d(line.get("amount"))
        if nature is None or amount == 0:
            continue
        out.append(PostingLine(str(line["code"]), str(line.get("name", "")), nature, amount,
                               cost_centre))
    tds = _d(payslip.tds)
    if tds:
        out.append(PostingLine(TDS, "Income tax deducted at source", LedgerNature.DEDUCTION, tds,
                               cost_centre))
    for line in payslip.input_lines or []:
        if line.get("skipped"):
            continue
        nature = _INPUT_NATURE.get(str(line.get("kind", "")))
        amount = _d(line.get("amount"))
        if nature is None or amount == 0:
            continue
        out.append(PostingLine(str(line["code"]), str(line.get("name", "")), nature, amount,
                               cost_centre))
    net = _d(payslip.net_pay)
    if net:
        out.append(PostingLine(NET_PAY, "Net salary payable", LedgerNature.NET_PAY, net,
                               cost_centre))
    return out


def required_codes(postings: list[PostingLine]) -> dict[str, tuple[LedgerNature, str]]:
    """The line codes a run needs mapped, with their nature and a readable name."""
    codes: dict[str, tuple[LedgerNature, str]] = {}
    for posting in postings:
        codes.setdefault(posting.line_code, (posting.nature, posting.name))
    return codes


def _missing(mapping: LedgerMapping | None, nature: LedgerNature) -> bool:
    if mapping is None:
        return True
    needs_debit = nature in (LedgerNature.EARNING, LedgerNature.EMPLOYER_CONTRIBUTION)
    needs_credit = nature in (
        LedgerNature.DEDUCTION, LedgerNature.EMPLOYER_CONTRIBUTION, LedgerNature.NET_PAY
    )
    return (needs_debit and not mapping.debit_ledger) or (
        needs_credit and not mapping.credit_ledger
    )


def build_journal(
    postings: list[PostingLine], mappings: dict[str, LedgerMapping]
) -> tuple[list[JournalLine], list[str]]:
    """Map postings to ledgers and net them per ledger and cost centre.

    Returns the journal lines and the codes that have no usable mapping. A
    negative amount (an arrear refund of an employer contribution, say) posts to
    the opposite side rather than as a negative debit.
    """
    missing = sorted(
        {p.line_code for p in postings if _missing(mappings.get(p.line_code), p.nature)}
    )
    if missing:
        return [], missing

    debit: dict[tuple[str, str | None], Decimal] = defaultdict(lambda: ZERO)
    credit: dict[tuple[str, str | None], Decimal] = defaultdict(lambda: ZERO)
    codes: dict[tuple[str, str | None], set[str]] = defaultdict(set)

    def post(side: dict, ledger: str, amount: Decimal, cc: str | None, code: str) -> None:
        if amount < 0:
            other = credit if side is debit else debit
            other[(ledger, cc)] += -amount
        else:
            side[(ledger, cc)] += amount
        codes[(ledger, cc)].add(code)

    for p in postings:
        mapping = mappings[p.line_code]
        if p.nature is LedgerNature.EARNING:
            post(debit, mapping.debit_ledger, p.amount, p.cost_centre, p.line_code)  # type: ignore[arg-type]
        elif p.nature in (LedgerNature.DEDUCTION, LedgerNature.NET_PAY):
            post(credit, mapping.credit_ledger, p.amount, p.cost_centre, p.line_code)  # type: ignore[arg-type]
        else:
            post(debit, mapping.debit_ledger, p.amount, p.cost_centre, p.line_code)  # type: ignore[arg-type]
            post(credit, mapping.credit_ledger, p.amount, p.cost_centre, p.line_code)  # type: ignore[arg-type]

    lines: list[JournalLine] = []
    for key in sorted(set(debit) | set(credit), key=lambda k: (k[0], k[1] or "")):
        net = debit.get(key, ZERO) - credit.get(key, ZERO)
        if net == 0:
            continue
        ledger, cc = key
        lines.append(
            JournalLine(
                ledger=ledger,
                debit=net.quantize(CENT) if net > 0 else ZERO,
                credit=(-net).quantize(CENT) if net < 0 else ZERO,
                cost_centre=cc,
                line_codes=codes[key],
            )
        )
    total_debit = sum((line.debit for line in lines), start=ZERO)
    total_credit = sum((line.credit for line in lines), start=ZERO)
    if total_debit != total_credit:
        raise ValueError(
            f"The journal does not balance: debits {total_debit}, credits {total_credit}. A "
            "payslip on this run does not add up; recompute it before posting."
        )
    return lines, []


# ── Serialisers (pure) ──────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class VoucherMeta:
    voucher_number: str
    voucher_date: date
    narration: str
    company_name: str


def _csv(lines: list[JournalLine], meta: VoucherMeta) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["Date", "Voucher number", "Ledger", "Debit", "Credit", "Cost centre", "Narration"]
    )
    for line in lines:
        writer.writerow(
            [
                meta.voucher_date.isoformat(),
                meta.voucher_number,
                line.ledger,
                f"{line.debit:.2f}" if line.debit else "",
                f"{line.credit:.2f}" if line.credit else "",
                line.cost_centre or "",
                meta.narration,
            ]
        )
    return buffer.getvalue()


def _zoho(lines: list[JournalLine], meta: VoucherMeta) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "Journal Date", "Journal Number", "Reference Number", "Notes", "Journal Type",
            "Currency", "Account", "Description", "Debit", "Credit",
        ]
    )
    for line in lines:
        writer.writerow(
            [
                meta.voucher_date.isoformat(),
                meta.voucher_number,
                meta.voucher_number,
                meta.narration,
                "both",
                "INR",
                line.ledger,
                line.cost_centre or "",
                f"{line.debit:.2f}" if line.debit else "",
                f"{line.credit:.2f}" if line.credit else "",
            ]
        )
    return buffer.getvalue()


def _tally(lines: list[JournalLine], meta: VoucherMeta) -> str:
    by_ledger: dict[str, list[JournalLine]] = defaultdict(list)
    for line in lines:
        by_ledger[line.ledger].append(line)

    entries: list[str] = []
    for ledger in sorted(by_ledger):
        group = by_ledger[ledger]
        net = sum((line.debit - line.credit for line in group), start=ZERO)
        if net == 0:
            continue
        is_debit = net > 0
        # Tally: a debit is a negative amount and "deemed positive"; a credit the reverse.
        amount = -net
        allocations = ""
        centres = [line for line in group if line.cost_centre]
        if centres:
            parts = "".join(
                "<COSTCENTREALLOCATIONS.LIST>"
                f"<NAME>{escape(line.cost_centre or '')}</NAME>"
                f"<AMOUNT>{-(line.debit - line.credit):.2f}</AMOUNT>"
                "</COSTCENTREALLOCATIONS.LIST>"
                for line in centres
            )
            allocations = (
                "<CATEGORYALLOCATIONS.LIST><CATEGORY>Primary Cost Category</CATEGORY>"
                f"{parts}</CATEGORYALLOCATIONS.LIST>"
            )
        entries.append(
            "<ALLLEDGERENTRIES.LIST>"
            f"<LEDGERNAME>{escape(ledger)}</LEDGERNAME>"
            f"<ISDEEMEDPOSITIVE>{'Yes' if is_debit else 'No'}</ISDEEMEDPOSITIVE>"
            f"<AMOUNT>{amount:.2f}</AMOUNT>"
            f"{allocations}"
            "</ALLLEDGERENTRIES.LIST>"
        )
    voucher = (
        '<VOUCHER VCHTYPE="Journal" ACTION="Create">'
        f"<DATE>{meta.voucher_date:%Y%m%d}</DATE>"
        "<VOUCHERTYPENAME>Journal</VOUCHERTYPENAME>"
        f"<VOUCHERNUMBER>{escape(meta.voucher_number)}</VOUCHERNUMBER>"
        f"<NARRATION>{escape(meta.narration)}</NARRATION>"
        f"{''.join(entries)}"
        "</VOUCHER>"
    )
    return (
        "<ENVELOPE><HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>"
        "<BODY><IMPORTDATA><REQUESTDESC><REPORTNAME>Vouchers</REPORTNAME>"
        "<STATICVARIABLES>"
        f"<SVCURRENTCOMPANY>{escape(meta.company_name)}</SVCURRENTCOMPANY>"
        "</STATICVARIABLES></REQUESTDESC>"
        f'<REQUESTDATA><TALLYMESSAGE xmlns:UDF="TallyUDF">{voucher}</TALLYMESSAGE>'
        "</REQUESTDATA></IMPORTDATA></BODY></ENVELOPE>"
    )


def serialise(fmt: str, lines: list[JournalLine], meta: VoucherMeta) -> tuple[str, str, str]:
    """(content, media type, file extension)."""
    if fmt == JournalFormat.TALLY:
        return _tally(lines, meta), "application/xml", "xml"
    if fmt == JournalFormat.ZOHO:
        return _zoho(lines, meta), "text/csv", "csv"
    return _csv(lines, meta), "text/csv", "csv"


# ── Service ─────────────────────────────────────────────────────────────────


async def _run_postings(
    session: AsyncSession, user: User, run: PayrollRun, *, by_department: bool
) -> list[PostingLine]:
    rows = (
        await session.execute(
            tenant_scoped(
                select(Payslip, Employee.department_id)
                .join(Employee, Payslip.employee_id == Employee.id)
                .where(Payslip.run_id == run.id),
                Payslip,
                user,
            )
        )
    ).all()
    departments: dict[uuid.UUID, str] = {}
    if by_department:
        departments = {
            row.id: row.name
            for row in (
                await session.scalars(tenant_scoped(select(Department), Department, user))
            ).all()
        }
    postings: list[PostingLine] = []
    for payslip, department_id in rows:
        centre = departments.get(department_id) if by_department and department_id else None
        if by_department and centre is None:
            centre = "Unassigned"
        postings.extend(postings_for(payslip, centre))
    return postings


async def _mappings(session: AsyncSession, user: User) -> dict[str, LedgerMapping]:
    return {
        row.line_code: row
        for row in (
            await session.scalars(tenant_scoped(select(LedgerMapping), LedgerMapping, user))
        ).all()
    }


async def _approved_run(session: AsyncSession, user: User, run_id: uuid.UUID) -> PayrollRun:
    run = await session.scalar(
        tenant_scoped(select(PayrollRun).where(PayrollRun.id == run_id), PayrollRun, user)
    )
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    if run.status not in (PayrollRunStatus.APPROVED, PayrollRunStatus.PAID):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only an approved run is posted to the books; this one can still change.",
        )
    return run


async def mapping_status(
    session: AsyncSession, user: User, run_id: uuid.UUID | None = None
) -> list[dict]:
    """Every line code the tenant has mapped, plus a run's unmapped ones."""
    permissions.require_hr(user, detail="Ledger mappings are payroll's.")
    mappings = await _mappings(session, user)
    needed: dict[str, tuple[LedgerNature, str]] = {}
    if run_id is not None:
        run = await _approved_run(session, user, run_id)
        needed = required_codes(await _run_postings(session, user, run, by_department=False))
    out: list[dict] = []
    for code in sorted(set(mappings) | set(needed)):
        mapping = mappings.get(code)
        nature = mapping.nature if mapping else needed[code][0]
        out.append(
            {
                "line_code": code,
                "name": needed.get(code, (nature, code))[1],
                "nature": nature.value,
                "debit_ledger": mapping.debit_ledger if mapping else None,
                "credit_ledger": mapping.credit_ledger if mapping else None,
                "complete": not _missing(mapping, nature),
                "used_by_run": code in needed,
            }
        )
    return out


async def save_mappings(
    session: AsyncSession,
    user: User,
    rows: list[dict],
    *,
    request: Request | None = None,
) -> list[dict]:
    permissions.require_hr(user, detail="Ledger mappings are payroll's.")
    existing = await _mappings(session, user)
    changed: list[str] = []
    for row in rows:
        code = str(row["line_code"]).strip().upper()
        nature = LedgerNature(row["nature"])
        debit = (row.get("debit_ledger") or "").strip() or None
        credit = (row.get("credit_ledger") or "").strip() or None
        mapping = existing.get(code)
        if mapping is None:
            mapping = LedgerMapping(tenant_id=user.tenant_id, line_code=code, nature=nature)
            session.add(mapping)
            existing[code] = mapping
        if (mapping.nature, mapping.debit_ledger, mapping.credit_ledger) != (nature, debit, credit):
            changed.append(code)
        mapping.nature = nature
        mapping.debit_ledger = debit
        mapping.credit_ledger = credit
        mapping.updated_by_user_id = user.id
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="ledger_mapping",
        actor=user,
        changes={"codes": changed},
        summary=f"Updated the ledger mapping for {len(changed)} payslip line(s)",
        request=request,
    )
    await session.commit()
    return await mapping_status(session, user)


async def export_journal(
    session: AsyncSession,
    user: User,
    run_id: uuid.UUID,
    *,
    fmt: str,
    by_department: bool = False,
    request: Request | None = None,
) -> tuple[str, str, str]:
    """(content, media type, file name) for an approved run's journal. Audited."""
    permissions.require_hr(user, detail="The payroll journal is payroll's.")
    if fmt not in JournalFormat.ALL:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown format '{fmt}'. Choose csv, tally or zoho.",
        )
    run = await _approved_run(session, user, run_id)
    postings = await _run_postings(session, user, run, by_department=by_department)
    if not postings:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="This run has no payslips to post."
        )
    try:
        lines, missing = build_journal(postings, await _mappings(session, user))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if missing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"{len(missing)} payslip line(s) have no ledger yet: {', '.join(missing)}. "
                "Map them under Payroll → Accounting before exporting, so nothing posts to a "
                "ledger your books do not have."
            ),
        )
    entity = await session.get(LegalEntity, run.legal_entity_id)
    company = entity.name if entity is not None and entity.tenant_id == user.tenant_id else ""
    type_label = run.run_type.value.replace("_", "-")
    meta = VoucherMeta(
        voucher_number=f"PAY-{run.period_year}-{run.period_month:02d}-{str(run.id)[:8]}",
        voucher_date=run.payment_date or run.effective_date,
        narration=(
            f"Payroll for {run.period_month:02d}/{run.period_year} ({type_label} run), "
            f"{run.employee_count} employee(s)"
        ),
        company_name=company,
    )
    content, media_type, extension = serialise(fmt, lines, meta)
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.EXPORT,
        entity_type="payroll_run",
        entity_id=run.id,
        actor=user,
        changes={"journal_format": fmt, "lines": len(lines), "by_department": by_department},
        summary=f"Exported the payroll journal for {run.period_month:02d}/{run.period_year}",
        request=request,
    )
    await session.commit()
    stamp = datetime.now(UTC).strftime("%Y%m%d")
    return content, media_type, f"payroll-journal-{meta.voucher_number}-{stamp}.{extension}"
