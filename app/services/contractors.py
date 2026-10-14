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
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from fastapi import HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.contractors import (
    Contractor,
    ContractorInvoice,
    ContractorKind,
    InvoiceStatus,
    TdsSection,
)
from app.models.payroll import PayrollRuleSet
from app.models.user import User
from app.services import audit

__all__ = [
    "ContractorIn",
    "InvoiceIn",
    "approve_invoice",
    "create_contractor",
    "create_invoice",
    "form_26q_csv",
    "list_contractors",
    "list_invoices",
    "mark_paid",
]

RULE_TYPE = "tds_non_salary"
#: 26Q's section codes.
_26Q_CODE = {
    TdsSection.S194C: "94C",
    TdsSection.S194J_PROFESSIONAL: "94J",
    TdsSection.S194J_TECHNICAL: "94J",
}
_ONE = Decimal(1)
_HUNDRED = Decimal(100)


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


def _fy(day: date) -> tuple[date, date]:
    start = date(day.year if day.month >= 4 else day.year - 1, 4, 1)
    return start, date(start.year + 1, 3, 31)


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


async def _rules_on(session: AsyncSession, user: User, day: date) -> PayrollRuleSet:
    row = await session.scalar(
        tenant_scoped(
            select(PayrollRuleSet)
            .where(
                PayrollRuleSet.rule_type == RULE_TYPE,
                PayrollRuleSet.approved_at.is_not(None),
                PayrollRuleSet.effective_from <= day,
                or_(PayrollRuleSet.effective_to.is_(None), PayrollRuleSet.effective_to >= day),
            )
            .order_by(PayrollRuleSet.effective_from.desc())
            .limit(1),
            PayrollRuleSet,
            user,
        )
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"No approved TDS rule set for contractor payments covers {day}. Add a "
                f"'{RULE_TYPE}' rule set under Payroll > Rule sets first."
            ),
        )
    return row


def _rate(rules: dict[str, Any], contractor: Contractor, section: TdsSection) -> Decimal:
    if not contractor.pan:
        return _d(rules["no_pan_rate"])
    spec = rules["sections"][section.value]
    if section is TdsSection.S194C:
        individual = contractor.kind in (ContractorKind.INDIVIDUAL, ContractorKind.HUF)
        return _d(spec["rate_individual"] if individual else spec["rate_other"])
    return _d(spec["rate"])


async def _untaxed_this_year(
    session: AsyncSession, user: User, contractor: Contractor, section: TdsSection, day: date
) -> tuple[Decimal, Decimal]:
    """(everything invoiced this year so far, of which nothing was taxed yet)."""
    start, end = _fy(day)
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
    total = sum((_d(r.amount) for r in rows), start=Decimal(0))
    taxed = sum((_d(r.tds_base) for r in rows), start=Decimal(0))
    return total, max(Decimal(0), total - taxed)


async def _compute(
    session: AsyncSession,
    user: User,
    contractor: Contractor,
    section: TdsSection,
    amount: Decimal,
    day: date,
) -> tuple[Decimal, Decimal, Decimal, str | None, PayrollRuleSet]:
    """(base, rate, tds, note, rule set) for one new invoice."""
    rule_set = await _rules_on(session, user, day)
    rules = rule_set.rules or {}
    try:
        spec = rules["sections"][section.value]
        rate = _rate(rules, contractor, section)
    except (KeyError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"The '{RULE_TYPE}' rule set has no entry for section {section.value}.",
        ) from exc
    year_total, untaxed_before = await _untaxed_this_year(session, user, contractor, section, day)
    annual = _d(spec.get("annual_threshold", "0"))
    single = _d(spec["single_threshold"]) if "single_threshold" in spec else None
    after = year_total + amount
    if single is not None and amount > single:
        base, note = amount, None
        if after > annual:
            base, note = amount + untaxed_before, None
    elif after > annual:
        base = amount + untaxed_before
        note = (
            "The year's total crossed the threshold, so earlier untaxed invoices are taxed now."
            if untaxed_before > 0
            else None
        )
    else:
        return (
            Decimal(0),
            Decimal(0),
            Decimal(0),
            (f"No TDS: below the {section.value} threshold so far this year."),
            rule_set,
        )
    tds = (base * rate / _HUNDRED).quantize(_ONE, rounding=ROUND_HALF_UP)
    if not contractor.pan:
        note = (note + " " if note else "") + "No PAN on file: the higher rate applies."
    return base, rate, tds, note, rule_set


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
    base, rate, tds, note, rule_set = await _compute(
        session, user, contractor, section, payload.amount, payload.invoice_date
    )
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
    date and amounts the 26Q annexure asks for, from invoices credited in the
    quarter. Validation by the FVU and filing stay with the deductor.
    """
    permissions.require_hr(user, detail="26Q is for HR and finance.")
    if quarter not in _QUARTERS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Quarter 1 to 4."
        )
    start_year = int(financial_year[:4])
    first, last = _QUARTERS[quarter]
    year = start_year + (1 if quarter == 4 else 0)
    start = date(year, first, 1)
    end = date(year, last, 30 if last in (6, 9) else 31)
    rows = (
        await session.execute(
            tenant_scoped(
                select(ContractorInvoice, Contractor)
                .join(Contractor, Contractor.id == ContractorInvoice.contractor_id)
                .where(
                    ContractorInvoice.invoice_date >= start,
                    ContractorInvoice.invoice_date <= end,
                    ContractorInvoice.status.in_([InvoiceStatus.APPROVED, InvoiceStatus.PAID]),
                )
                .order_by(ContractorInvoice.invoice_date),
                ContractorInvoice,
                user,
            )
        )
    ).all()
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
        ]
    )
    for invoice, contractor in rows:
        writer.writerow(
            [
                contractor.pan or "PANNOTAVBL",
                contractor.name,
                "01" if contractor.kind is ContractorKind.COMPANY else "02",
                _26Q_CODE[invoice.section],
                invoice.invoice_date.isoformat(),
                str(invoice.amount),
                str(invoice.tds_rate),
                str(invoice.tds_amount),
                invoice.paid_on.isoformat() if invoice.paid_on else "",
                invoice.invoice_number,
                "C" if not contractor.pan else "",
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
