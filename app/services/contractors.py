"""Contractors, their invoices, TDS under 194C / 194J, and the 26Q working file.

TDS is computed when an invoice is entered, from the approved
`tds_non_salary` rule set in force on the invoice date — nothing here is a
rate. The rule set's shape:

    {
      "no_pan_rate": "20",
      "sections": {
        "194C": {"rate_individual": "1", "rate_other": "2",
                 "single_threshold": "30000", "annual_threshold": "100000"},
        "194J_professional": {"rate": "10", "annual_threshold": "50000"},
        "194J_technical": {"rate": "2", "annual_threshold": "50000"}
      }
    }

Thresholds: below them nothing is deducted, and the invoice says why. When the
year's total crosses the annual threshold, TDS is charged on everything paid
that year to that contractor under that section and not yet taxed — the
catch-up the Act requires — so the invoice that crosses carries it.

TDS is on the amount before GST, where GST is shown separately, and is rounded
to the rupee. A contractor without a PAN is charged the higher no-PAN rate.
"""

from __future__ import annotations

import csv
import io
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.consultant_payouts import (
    ConsultantPayoutLine,
    ConsultantPayoutRun,
    PayoutRunStatus,
)
from app.models.contractors import (
    Contractor,
    ContractorInvoice,
    ContractorKind,
    InvoiceStatus,
    TdsSection,
)
from app.models.employee import Employee
from app.models.filings import TdsChallan
from app.models.org import LegalEntity
from app.models.payroll import PayrollRuleSet
from app.models.tenant import Tenant
from app.models.user import User
from app.services import audit, tds_non_salary

__all__ = [
    "ContractorIn",
    "InvoiceIn",
    "approve_invoice",
    "challan_position",
    "consultant_tds_statement_pdf",
    "create_contractor",
    "create_invoice",
    "form_26q_csv",
    "link_challan",
    "list_contractors",
    "list_invoices",
    "mark_paid",
    "tds_statement_pdf",
]

#: 26Q's section codes. Since FY 2020-21 section 194J is split in the statement:
#: 194J(a), fees for technical services, and 194J(b), professional fees.
_26Q_CODE = {
    TdsSection.S194C: "94C",
    TdsSection.S194J_PROFESSIONAL: "4JB",
    TdsSection.S194J_TECHNICAL: "4JA",
}


class ContractorIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=2, max_length=200)
    kind: ContractorKind
    pan: str | None = Field(default=None, pattern=r"^[A-Z]{5}[0-9]{4}[A-Z]$")
    gstin: str | None = Field(default=None, pattern=r"^[0-9]{2}[A-Z0-9]{13}$")
    email: str | None = Field(default=None, max_length=255)
    default_section: TdsSection


class InvoiceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invoice_number: str = Field(min_length=1, max_length=64)
    invoice_date: date
    description: str | None = Field(default=None, max_length=2000)
    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    gst_amount: Decimal = Field(default=Decimal("0"), ge=0, max_digits=14, decimal_places=2)
    section: TdsSection | None = None


def _d(value: Any) -> Decimal:
    return Decimal(str(value))


def _money(value: Any) -> str:
    return str(_d(value).quantize(Decimal("0.01")))


def _contractor_read(c: Contractor) -> dict[str, Any]:
    pan = c.pan or None
    return {
        "id": str(c.id),
        "name": c.name,
        "kind": c.kind.value,
        # The contractor screens are admin/sub-admin only: the PAN in full.
        "pan": pan,
        "pan_masked": f"{pan[:2]}XXXXX{pan[-3:]}" if pan else None,
        "has_pan": bool(pan),
        "gstin": c.gstin,
        "email": c.email,
        "default_section": c.default_section.value,
        "active": c.active,
    }


def _invoice_read(i: ContractorInvoice, contractor: Contractor | None = None) -> dict[str, Any]:
    return {
        "id": str(i.id),
        "contractor_id": str(i.contractor_id),
        "contractor_name": contractor.name if contractor else None,
        "invoice_number": i.invoice_number,
        "invoice_date": i.invoice_date.isoformat(),
        "description": i.description,
        "amount": _money(i.amount),
        "gst_amount": _money(i.gst_amount),
        "section": i.section.value,
        "tds_base": _money(i.tds_base),
        "tds_rate": str(_d(i.tds_rate).quantize(Decimal("0.001"))),
        "tds_amount": _money(i.tds_amount),
        "net_payable": _money(i.net_payable),
        "tds_note": i.tds_note,
        "status": i.status.value,
        "paid_on": i.paid_on.isoformat() if i.paid_on else None,
        "payment_reference": i.payment_reference,
        "challan_id": str(i.challan_id) if i.challan_id else None,
    }


# ── Contractors ─────────────────────────────────────────────────────────────


async def create_contractor(
    session: AsyncSession, user: User, payload: ContractorIn, *, request: Request | None
) -> dict[str, Any]:
    permissions.require_hr(user, detail="Contractors are set up by HR or finance.")
    row = Contractor(tenant_id=user.tenant_id, **payload.model_dump())
    session.add(row)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="contractor",
        entity_id=row.id,
        actor=user,
        changes={"name": row.name, "kind": row.kind.value, "section": row.default_section.value},
        summary=f"Added the contractor {row.name}",
        request=request,
    )
    await session.commit()
    return _contractor_read(row)


async def list_contractors(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    permissions.require_hr(user, detail="Contractors are for HR and finance.")
    rows = (
        await session.scalars(
            tenant_scoped(select(Contractor).order_by(Contractor.name), Contractor, user)
        )
    ).all()
    return [_contractor_read(r) for r in rows]


async def _contractor(session: AsyncSession, user: User, contractor_id: uuid.UUID) -> Contractor:
    row = await session.scalar(
        tenant_scoped(select(Contractor).where(Contractor.id == contractor_id), Contractor, user)
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Contractor not found")
    return row


# ── TDS ─────────────────────────────────────────────────────────────────────


async def _year_to_date(
    session: AsyncSession, user: User, contractor: Contractor, section: TdsSection, day: date
) -> tds_non_salary.YearToDate:
    """Everything invoiced this year so far, and how much of it was already taxed."""
    start, end = tds_non_salary.fy_bounds(day)
    rows = (
        await session.scalars(
            tenant_scoped(
                select(ContractorInvoice).where(
                    ContractorInvoice.contractor_id == contractor.id,
                    ContractorInvoice.section == section,
                    ContractorInvoice.invoice_date >= start,
                    ContractorInvoice.invoice_date <= end,
                    ContractorInvoice.status != InvoiceStatus.CANCELLED,
                ),
                ContractorInvoice,
                user,
            )
        )
    ).all()
    return tds_non_salary.YearToDate(
        total=sum((_d(r.amount) for r in rows), start=Decimal(0)),
        taxed_base=sum((_d(r.tds_base) for r in rows), start=Decimal(0)),
    )


async def _compute(
    session: AsyncSession,
    user: User,
    contractor: Contractor,
    section: TdsSection,
    amount: Decimal,
    day: date,
) -> tuple[tds_non_salary.TdsResult, PayrollRuleSet]:
    """TDS for one new invoice, and the rule set it used."""
    rule_set = await tds_non_salary.rules_on(session, user, day)
    payee = tds_non_salary.Payee(pan=contractor.pan, kind=contractor.kind, section=section)
    ytd = await _year_to_date(session, user, contractor, section, day)
    return tds_non_salary.compute(rule_set, payee, amount, ytd), rule_set


# ── Invoices ────────────────────────────────────────────────────────────────


async def create_invoice(
    session: AsyncSession,
    user: User,
    contractor_id: uuid.UUID,
    payload: InvoiceIn,
    *,
    request: Request | None,
) -> dict[str, Any]:
    permissions.require_hr(user, detail="Invoices are entered by HR or finance.")
    contractor = await _contractor(session, user, contractor_id)
    if not contractor.active:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This contractor is off.")
    duplicate = await session.scalar(
        tenant_scoped(
            select(ContractorInvoice.id).where(
                ContractorInvoice.contractor_id == contractor.id,
                ContractorInvoice.invoice_number == payload.invoice_number.strip(),
            ),
            ContractorInvoice,
            user,
        )
    )
    if duplicate is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That invoice number is already entered for this contractor.",
        )
    section = payload.section or contractor.default_section
    result, rule_set = await _compute(
        session, user, contractor, section, payload.amount, payload.invoice_date
    )
    base, rate, tds, note = result.base, result.rate, result.tds, result.note
    row = ContractorInvoice(
        tenant_id=user.tenant_id,
        contractor_id=contractor.id,
        invoice_number=payload.invoice_number.strip(),
        invoice_date=payload.invoice_date,
        description=(payload.description or "").strip() or None,
        amount=payload.amount,
        gst_amount=payload.gst_amount,
        section=section,
        tds_base=base,
        tds_rate=rate,
        tds_amount=tds,
        net_payable=payload.amount + payload.gst_amount - tds,
        tds_note=note,
        rule_set_id=rule_set.id,
        created_by_user_id=user.id,
    )
    session.add(row)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="contractor_invoice",
        entity_id=row.id,
        actor=user,
        changes={
            "amount": str(row.amount),
            "section": section.value,
            "tds": str(tds),
            "rate": str(rate),
        },
        summary=f"Entered invoice {row.invoice_number} from {contractor.name}: TDS {tds}",
        request=request,
    )
    await session.commit()
    return _invoice_read(row, contractor)


async def list_invoices(
    session: AsyncSession, user: User, *, contractor_id: uuid.UUID | None = None
) -> list[dict[str, Any]]:
    permissions.require_hr(user, detail="Invoices are for HR and finance.")
    stmt = select(ContractorInvoice, Contractor).join(
        Contractor, Contractor.id == ContractorInvoice.contractor_id
    )
    if contractor_id:
        stmt = stmt.where(ContractorInvoice.contractor_id == contractor_id)
    rows = (
        await session.execute(
            tenant_scoped(
                stmt.order_by(ContractorInvoice.invoice_date.desc()).limit(500),
                ContractorInvoice,
                user,
            )
        )
    ).all()
    return [_invoice_read(i, c) for i, c in rows]


async def _invoice(session: AsyncSession, user: User, invoice_id: uuid.UUID) -> ContractorInvoice:
    row = await session.scalar(
        tenant_scoped(
            select(ContractorInvoice).where(ContractorInvoice.id == invoice_id),
            ContractorInvoice,
            user,
        )
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invoice not found")
    return row


async def approve_invoice(
    session: AsyncSession, user: User, invoice_id: uuid.UUID, *, request: Request | None
) -> dict[str, Any]:
    """Maker-checker: whoever entered an invoice does not approve it."""
    if not permissions.is_admin(user):
        raise permissions.forbidden("Only an admin approves contractor invoices.")
    row = await _invoice(session, user, invoice_id)
    if row.created_by_user_id == user.id:
        raise permissions.forbidden("You entered this invoice; somebody else approves it.")
    if row.status is not InvoiceStatus.PENDING:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Not pending.")
    row.status = InvoiceStatus.APPROVED
    row.approved_by_user_id = user.id
    row.approved_at = datetime.now(UTC)
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.APPROVE,
        entity_type="contractor_invoice",
        entity_id=row.id,
        actor=user,
        changes={"status": "approved"},
        summary=f"Approved invoice {row.invoice_number}",
        request=request,
    )
    await session.commit()
    return _invoice_read(row)


async def mark_paid(
    session: AsyncSession,
    user: User,
    invoice_id: uuid.UUID,
    *,
    paid_on: date,
    reference: str | None,
    request: Request | None,
) -> dict[str, Any]:
    permissions.require_hr(user, detail="Payments are recorded by HR or finance.")
    row = await _invoice(session, user, invoice_id)
    if row.status is not InvoiceStatus.APPROVED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Only an approved invoice is paid."
        )
    row.status = InvoiceStatus.PAID
    row.paid_on = paid_on
    row.payment_reference = (reference or "").strip() or None
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="contractor_invoice",
        entity_id=row.id,
        actor=user,
        changes={"status": "paid", "paid_on": paid_on.isoformat()},
        summary=f"Recorded payment of invoice {row.invoice_number}",
        request=request,
    )
    await session.commit()
    return _invoice_read(row)


@dataclass
class _Deduction:
    """One TDS line of the quarter, from a contractor invoice or a consultant payout."""

    pan: str | None
    name: str
    company: bool
    section: TdsSection
    credit_date: date
    amount: Decimal
    rate: Decimal
    tds: Decimal
    paid_on: date | None
    reference: str
    challan_id: uuid.UUID | None


async def _invoice_deductions(
    session: AsyncSession,
    user: User,
    start: date,
    end: date,
    *,
    contractor_id: uuid.UUID | None = None,
    tds_only: bool = False,
) -> list[_Deduction]:
    stmt = (
        select(ContractorInvoice, Contractor)
        .join(Contractor, Contractor.id == ContractorInvoice.contractor_id)
        .where(
            ContractorInvoice.invoice_date >= start,
            ContractorInvoice.invoice_date <= end,
            ContractorInvoice.status.in_([InvoiceStatus.APPROVED, InvoiceStatus.PAID]),
        )
    )
    if contractor_id is not None:
        stmt = stmt.where(ContractorInvoice.contractor_id == contractor_id)
    if tds_only:
        stmt = stmt.where(ContractorInvoice.tds_amount > 0)
    rows = (await session.execute(tenant_scoped(stmt, ContractorInvoice, user))).all()
    return [
        _Deduction(
            pan=contractor.pan,
            name=contractor.name,
            company=contractor.kind is ContractorKind.COMPANY,
            section=invoice.section,
            credit_date=invoice.invoice_date,
            amount=_d(invoice.amount),
            rate=_d(invoice.tds_rate),
            tds=_d(invoice.tds_amount),
            paid_on=invoice.paid_on,
            reference=invoice.invoice_number,
            challan_id=invoice.challan_id,
        )
        for invoice, contractor in rows
    ]


async def _consultant_deductions(
    session: AsyncSession,
    user: User,
    start: date,
    end: date,
    *,
    employee_id: uuid.UUID | None = None,
) -> list[_Deduction]:
    """Consultant payout lines that deducted TDS, dated by the run's pay date.

    What was actually deducted is reported (`tds_amount`), never what was owed
    (`tds_due`): TDS carried forward is reported on the payout that takes it.
    """
    stmt = (
        select(ConsultantPayoutLine, ConsultantPayoutRun, Employee)
        .join(ConsultantPayoutRun, ConsultantPayoutRun.id == ConsultantPayoutLine.run_id)
        .join(Employee, Employee.id == ConsultantPayoutLine.employee_id)
        .where(
            ConsultantPayoutRun.pay_date >= start,
            ConsultantPayoutRun.pay_date <= end,
            ConsultantPayoutRun.status.in_([PayoutRunStatus.APPROVED, PayoutRunStatus.PAID]),
            ConsultantPayoutLine.excluded.is_(False),
            ConsultantPayoutLine.tds_amount > 0,
        )
    )
    if employee_id is not None:
        stmt = stmt.where(ConsultantPayoutLine.employee_id == employee_id)
    rows = (await session.execute(tenant_scoped(stmt, ConsultantPayoutLine, user))).all()
    return [
        _Deduction(
            pan=person.pan,
            name=person.full_name,
            company=person.payee_kind is ContractorKind.COMPANY,
            section=line.section,
            credit_date=run.pay_date,
            amount=_d(line.gross),
            rate=_d(line.tds_rate),
            tds=_d(line.tds_amount),
            paid_on=run.paid_on,
            reference=f"Payout {run.pay_date.isoformat()}",
            challan_id=line.challan_id,
        )
        for line, run, person in rows
    ]


async def _deductions(
    session: AsyncSession, user: User, start: date, end: date, *, tds_only: bool = False
) -> list[_Deduction]:
    """Every TDS line in a date range: contractor invoices and consultant payouts."""
    found = await _invoice_deductions(session, user, start, end, tds_only=tds_only)
    found += await _consultant_deductions(session, user, start, end)
    return sorted(found, key=lambda row: row.credit_date)


_QUARTERS = {1: (4, 6), 2: (7, 9), 3: (10, 12), 4: (1, 3)}


async def form_26q_csv(
    session: AsyncSession,
    user: User,
    *,
    financial_year: str,
    quarter: int,
    request: Request | None = None,
) -> str:
    """The quarter's deductee lines, for the return-preparation utility.

    A working file, not the FVU text file: the deductee details, section code,
    date and amounts the 26Q annexure asks for, from contractor invoices and
    consultant payouts credited in the quarter. Validation by the FVU and filing
    stay with the deductor.
    """
    permissions.require_hr(user, detail="26Q is for HR and finance.")
    if quarter not in _QUARTERS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Quarter 1 to 4."
        )
    start, end = _quarter_bounds(financial_year, quarter)
    challans = await _challans_by_id(session, user, financial_year, quarter)
    rows = await _deductions(session, user, start, end)
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(
        [
            "Deductee PAN",
            "Deductee name",
            "Deductee type",
            "Section code",
            "Date of credit",
            "Amount credited",
            "TDS rate (%)",
            "TDS deducted",
            "Date of payment",
            "Invoice number",
            "Remark",
            "Challan BSR code",
            "Challan date",
            "Challan serial",
        ]
    )
    for row in rows:
        challan = challans.get(row.challan_id) if row.challan_id else None
        writer.writerow(
            [
                row.pan or "PANNOTAVBL",
                row.name,
                "01" if row.company else "02",
                _26Q_CODE[row.section],
                row.credit_date.isoformat(),
                str(row.amount),
                str(row.rate),
                str(row.tds),
                row.paid_on.isoformat() if row.paid_on else "",
                row.reference,
                "C" if not row.pan else "",
                challan.bsr_code if challan else "",
                challan.deposit_date.isoformat() if challan else "",
                challan.challan_serial if challan else "",
            ]
        )
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.EXPORT,
        entity_type="form_26q",
        actor=user,
        changes={"financial_year": financial_year, "quarter": quarter, "rows": len(rows)},
        summary=f"Exported the 26Q working file for {financial_year} Q{quarter}",
        request=request,
    )
    await session.commit()
    return buffer.getvalue()


# ── Challans and deduction statements ───────────────────────────────────────


def _quarter_bounds(financial_year: str, quarter: int) -> tuple[date, date]:
    """First and last day of a quarter of a financial year written 2026-27 or 2026-2027."""
    if quarter not in _QUARTERS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Quarter 1 to 4."
        )
    first, last = _QUARTERS[quarter]
    year = int(financial_year[:4]) + (1 if quarter == 4 else 0)
    return date(year, first, 1), date(year, last, 30 if last in (6, 9) else 31)


def _quarter_of(day: date) -> tuple[int, int]:
    """(financial year's first calendar year, quarter) for a date."""
    start_year = day.year if day.month >= 4 else day.year - 1
    quarter = {4: 1, 5: 1, 6: 1, 7: 2, 8: 2, 9: 2, 10: 3, 11: 3, 12: 3}.get(day.month, 4)
    return start_year, quarter


async def _challans_by_id(
    session: AsyncSession, user: User, financial_year: str, quarter: int
) -> dict[uuid.UUID, TdsChallan]:
    rows = (
        await session.scalars(
            tenant_scoped(
                select(TdsChallan).where(
                    TdsChallan.form == "26Q",
                    TdsChallan.quarter == quarter,
                    TdsChallan.financial_year.like(f"{financial_year[:4]}-%"),
                ),
                TdsChallan,
                user,
            )
        )
    ).all()
    return {row.id: row for row in rows}


async def _linked_total(
    session: AsyncSession,
    user: User,
    challan_id: uuid.UUID,
    *,
    excluding_invoice: uuid.UUID | None = None,
    excluding_line: uuid.UUID | None = None,
) -> Decimal:
    """TDS already tied to a challan, from invoices and consultant payout lines."""
    invoices = select(ContractorInvoice.tds_amount).where(
        ContractorInvoice.challan_id == challan_id
    )
    if excluding_invoice is not None:
        invoices = invoices.where(ContractorInvoice.id != excluding_invoice)
    lines = select(ConsultantPayoutLine.tds_amount).where(
        ConsultantPayoutLine.challan_id == challan_id
    )
    if excluding_line is not None:
        lines = lines.where(ConsultantPayoutLine.id != excluding_line)
    amounts = list((await session.scalars(tenant_scoped(invoices, ContractorInvoice, user))).all())
    amounts += (await session.scalars(tenant_scoped(lines, ConsultantPayoutLine, user))).all()
    return sum((_d(a) for a in amounts), Decimal(0))


async def _payout_line(
    session: AsyncSession, user: User, line_id: uuid.UUID
) -> tuple[ConsultantPayoutLine, ConsultantPayoutRun, Employee]:
    found = (
        await session.execute(
            tenant_scoped(
                select(ConsultantPayoutLine, ConsultantPayoutRun, Employee)
                .join(ConsultantPayoutRun, ConsultantPayoutRun.id == ConsultantPayoutLine.run_id)
                .join(Employee, Employee.id == ConsultantPayoutLine.employee_id)
                .where(ConsultantPayoutLine.id == line_id),
                ConsultantPayoutLine,
                user,
            )
        )
    ).first()
    if found is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payout line not found")
    return found[0], found[1], found[2]


async def link_challan(
    session: AsyncSession,
    user: User,
    invoice_id: uuid.UUID | None = None,
    challan_id: uuid.UUID | None = None,
    *,
    consultant_payout_line_id: uuid.UUID | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    """Say which 26Q challan an invoice's or a consultant payout line's TDS was deposited under.

    Give an invoice or a consultant payout line, not both; a challan of None
    unlinks. The challan must be a 26Q deposit for the quarter the TDS was
    deducted in (an invoice's date, a payout's pay date), and what is tied to it
    cannot add up to more TDS than it deposited.
    """
    permissions.require_hr(user, detail="Challans are recorded by HR or finance.")
    if (invoice_id is None) == (consultant_payout_line_id is None):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Give either an invoice or a consultant payout line, not both.",
        )
    target: Any
    excluding: dict[str, uuid.UUID]
    if invoice_id is not None:
        target = await _invoice(session, user, invoice_id)
        label = f"invoice {target.invoice_number}"
        what = "invoice"
        credit_date = target.invoice_date
        statuses_ok = target.status in (InvoiceStatus.APPROVED, InvoiceStatus.PAID)
        not_ready = "Only an approved or paid invoice has TDS to deposit."
        entity_type = "contractor_invoice"
        excluding = {"excluding_invoice": target.id}
    else:
        assert consultant_payout_line_id is not None
        target, run, person = await _payout_line(session, user, consultant_payout_line_id)
        label = f"the payout to {person.full_name} on {run.pay_date.isoformat()}"
        what = "payout"
        credit_date = run.pay_date
        statuses_ok = run.status in (PayoutRunStatus.APPROVED, PayoutRunStatus.PAID)
        not_ready = "Only an approved or paid payout run has TDS to deposit."
        entity_type = "consultant_payout_line"
        excluding = {"excluding_line": target.id}
    deducted = _d(target.tds_amount)
    if challan_id is None:
        target.challan_id = None
        summary = f"Unlinked {label} from its challan"
    else:
        if not statuses_ok:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=not_ready)
        if deducted <= 0:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"No TDS was deducted on this {what}, so there is no deposit to link.",
            )
        challan = await session.scalar(
            tenant_scoped(select(TdsChallan).where(TdsChallan.id == challan_id), TdsChallan, user)
        )
        if challan is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Challan not found")
        if challan.form != "26Q":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="That is a 24Q (salary) challan. Contractor TDS goes under a 26Q one.",
            )
        start_year, quarter = _quarter_of(credit_date)
        if int(challan.financial_year[:4]) != start_year or challan.quarter != quarter:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"This TDS was deducted in Q{quarter} of {start_year}-{start_year + 1}; "
                    f"the challan is for Q{challan.quarter} of {challan.financial_year}."
                ),
            )
        linked = await _linked_total(session, user, challan.id, **excluding)
        if linked + deducted > _d(challan.tds_amount):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"The challan deposited {_money(challan.tds_amount)} of TDS and "
                    f"{_money(linked)} is already tied to it; this {what}'s "
                    f"{_money(deducted)} does not fit."
                ),
            )
        target.challan_id = challan.id
        summary = f"Tied {label} to challan {challan.challan_serial}"
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type=entity_type,
        entity_id=target.id,
        actor=user,
        changes={"challan_id": str(challan_id) if challan_id else None},
        summary=summary,
        request=request,
    )
    await session.commit()
    if invoice_id is not None:
        return _invoice_read(target)
    return {
        "id": str(target.id),
        "challan_id": str(target.challan_id) if target.challan_id else None,
        "tds_amount": _money(target.tds_amount),
    }


async def challan_position(
    session: AsyncSession, user: User, *, financial_year: str, quarter: int
) -> dict[str, Any]:
    """For a quarter: each 26Q challan against what is tied to it, and TDS not yet tied.

    Counts contractor invoices and consultant payouts alike.
    """
    permissions.require_hr(user, detail="26Q is for HR and finance.")
    start, end = _quarter_bounds(financial_year, quarter)
    challans = await _challans_by_id(session, user, financial_year, quarter)
    deductions = await _deductions(session, user, start, end, tds_only=True)
    tied: dict[uuid.UUID, Decimal] = {}
    untied = Decimal(0)
    untied_count = 0
    for row in deductions:
        if row.challan_id in challans:
            tied[row.challan_id] = tied.get(row.challan_id, Decimal(0)) + row.tds
        else:
            untied += row.tds
            untied_count += 1
    return {
        "financial_year": financial_year,
        "quarter": quarter,
        "challans": [
            {
                "id": str(c.id),
                "bsr_code": c.bsr_code,
                "deposit_date": c.deposit_date.isoformat(),
                "challan_serial": c.challan_serial,
                "tds_amount": _money(c.tds_amount),
                "tied": _money(tied.get(c.id, 0)),
                "unused": _money(_d(c.tds_amount) - tied.get(c.id, Decimal(0))),
            }
            for c in sorted(challans.values(), key=lambda c: (c.deposit_date, c.challan_serial))
        ],
        "untied_invoices": untied_count,
        "untied_tds": _money(untied),
        "ready": untied_count == 0,
    }


async def tds_statement_pdf(
    session: AsyncSession,
    user: User,
    contractor_id: uuid.UUID,
    *,
    financial_year: str,
    quarter: int,
    request: Request | None = None,
) -> tuple[bytes, str]:
    """A quarter's TDS deductions for one contractor, with the challans they went under.

    Form 16A itself is downloaded from TRACES once the 26Q is processed; this
    statement carries the same lines so the contractor can match them against
    their Form 26AS before then.
    """
    permissions.require_hr(user, detail="TDS statements are issued by HR or finance.")
    contractor = await _contractor(session, user, contractor_id)
    start, end = _quarter_bounds(financial_year, quarter)
    rows = await _invoice_deductions(session, user, start, end, contractor_id=contractor.id)
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No approved invoices from this contractor in that quarter.",
        )
    return await _statement(
        session,
        user,
        rows,
        payee_name=contractor.name,
        pan=contractor.pan,
        entity_type="contractor_tds_statement",
        entity_id=contractor.id,
        financial_year=financial_year,
        quarter=quarter,
        request=request,
    )


async def consultant_tds_statement_pdf(
    session: AsyncSession,
    user: User,
    employee_id: uuid.UUID,
    *,
    financial_year: str,
    quarter: int,
    request: Request | None = None,
) -> tuple[bytes, str]:
    """A quarter's TDS deductions for one consultant, with the challans they went under."""
    permissions.require_hr(user, detail="TDS statements are issued by HR or finance.")
    person = await session.scalar(
        tenant_scoped(select(Employee).where(Employee.id == employee_id), Employee, user)
    )
    if person is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Consultant not found")
    start, end = _quarter_bounds(financial_year, quarter)
    rows = await _consultant_deductions(session, user, start, end, employee_id=person.id)
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No TDS was deducted from this consultant in that quarter.",
        )
    return await _statement(
        session,
        user,
        rows,
        payee_name=person.full_name,
        pan=person.pan,
        entity_type="consultant_tds_statement",
        entity_id=person.id,
        financial_year=financial_year,
        quarter=quarter,
        request=request,
    )


async def _statement(
    session: AsyncSession,
    user: User,
    rows: list[_Deduction],
    *,
    payee_name: str,
    pan: str | None,
    entity_type: str,
    entity_id: uuid.UUID,
    financial_year: str,
    quarter: int,
    request: Request | None,
) -> tuple[bytes, str]:
    from html import escape

    from app.services.documents.pdf import Letterhead, render_pdf

    start, end = _quarter_bounds(financial_year, quarter)
    challans = await _challans_by_id(session, user, financial_year, quarter)
    tenant = await session.get(Tenant, user.tenant_id)
    entity_ids = {c.legal_entity_id for c in challans.values()}
    entity = (
        await session.get(LegalEntity, next(iter(entity_ids))) if len(entity_ids) == 1 else None
    )
    deductor = entity.name if entity else (tenant.name if tenant else "Deductor")
    tan = entity.tan if entity and entity.tan else "—"
    pan = pan or "PANNOTAVBL"
    lines = []
    total_credit = total_tds = Decimal(0)
    for row in sorted(rows, key=lambda r: r.credit_date):
        challan = challans.get(row.challan_id) if row.challan_id else None
        total_credit += row.amount
        total_tds += row.tds
        lines.append(
            "<tr>"
            f"<td>{escape(row.reference)}</td>"
            f"<td>{row.credit_date.isoformat()}</td>"
            f"<td>{_26Q_CODE[row.section]}</td>"
            f"<td>{_money(row.amount)}</td>"
            f"<td>{row.rate.quantize(Decimal('0.001'))}%</td>"
            f"<td>{_money(row.tds)}</td>"
            f"<td>{escape(challan.bsr_code) if challan else 'Not yet deposited'}</td>"
            f"<td>{challan.deposit_date.isoformat() if challan else '-'}</td>"
            f"<td>{escape(challan.challan_serial) if challan else '-'}</td>"
            "</tr>"
        )
    html = f"""
<h2>TDS deduction statement</h2>
<p>Financial year {escape(financial_year)}, quarter {quarter}
({start.isoformat()} to {end.isoformat()})</p>
<p><b>Deductor:</b> {escape(deductor)} &nbsp; <b>TAN:</b> {escape(tan)}<br/>
<b>Deductee:</b> {escape(payee_name)} &nbsp; <b>PAN:</b> {escape(pan)}</p>
<table class="ctc-table">
<thead><tr><th>Ref.</th><th>Date of credit</th><th>Section</th><th>Amount credited</th>
<th>Rate</th><th>TDS</th><th>BSR code</th><th>Deposited on</th><th>Challan serial</th></tr></thead>
<tbody>{"".join(lines)}</tbody>
<tfoot><tr><td colspan="3">Total</td><td>{_money(total_credit)}</td><td></td>
<td>{_money(total_tds)}</td><td colspan="3"></td></tr></tfoot>
</table>
<p style="font-size:8.5pt">Form 16A is issued from the TRACES portal after the quarter's 26Q
is processed. This statement lists the same deductions and deposits so you can match them
against your Form 26AS in the meantime.</p>
"""
    data = render_pdf(
        merged_html=html,
        reference_number=f"TDS-{financial_year}-Q{quarter}",
        letterhead=Letterhead(company_name=escape(deductor)),
    )
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.EXPORT,
        entity_type=entity_type,
        entity_id=entity_id,
        actor=user,
        changes={"financial_year": financial_year, "quarter": quarter},
        summary=f"Issued the {financial_year} Q{quarter} TDS statement for {payee_name}",
        request=request,
    )
    await session.commit()
    safe = "".join(ch for ch in payee_name if ch.isalnum())[:40] or "payee"
    return data, f"TDS-{safe}-{financial_year}-Q{quarter}.pdf"
