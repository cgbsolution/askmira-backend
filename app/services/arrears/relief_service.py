"""Section 89(1) relief on an arrear batch, and its Form 10E (slice 12 §4).

`relief.py` does the comparison and nothing else. This module supplies what it
compares: the arrear allocated to the years it relates to (from the batch's
own per-period lines), and the tax on each year's income **under that year's own
approved rule set** — a 2024-25 slab never prices a 2026-27 arrear.

Taxable income for earlier years comes from whoever holds it: the employer's
records where it has them, the employee's declaration where it does not. It is
supplied with the request rather than guessed, and stored in the working so the
Form 10E shows what it was computed from.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.arrears import ArrearBatch, ArrearLine, SectionRelief
from app.models.audit import AuditAction
from app.models.documents import IssuedLetter
from app.models.employee import Employee
from app.models.user import User
from app.services import audit
from app.services.arrears import form_10e, relief
from app.services.documents import service as documents_service
from app.services.filings import support
from app.services.tax import computation
from app.services.tax import service as tax_service

ZERO = Decimal(0)


def short_year(start_year: int) -> str:
    """2026 → "2026-27"."""
    return f"{start_year}-{(start_year + 1) % 100:02d}"


def _start_year(label: str) -> int:
    try:
        return int(label.split("-")[0])
    except (ValueError, IndexError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"'{label}' is not a financial year.",
        ) from exc


def _receipt_start(batch: ArrearBatch) -> int:
    received = batch.approved_at.date() if batch.approved_at else date.today()
    return received.year if received.month >= 4 else received.year - 1


async def _batch(session: AsyncSession, user: User, batch_id: uuid.UUID) -> ArrearBatch:
    batch = await session.scalar(
        tenant_scoped(select(ArrearBatch).where(ArrearBatch.id == batch_id), ArrearBatch, user)
    )
    if batch is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Arrear batch not found")
    return batch


async def _earlier_years(
    session: AsyncSession, user: User, batch: ArrearBatch, employee_id: uuid.UUID
) -> tuple[int, dict[int, Decimal]]:
    """The receipt year, and the earnings arrear owed to each earlier year."""
    lines = (
        await session.scalars(
            tenant_scoped(
                select(ArrearLine).where(
                    ArrearLine.batch_id == batch.id,
                    ArrearLine.employee_id == employee_id,
                    ArrearLine.component_type == "earning",
                ),
                ArrearLine,
                user,
            )
        )
    ).all()
    if not lines:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="This batch has no earnings arrear for this employee.",
        )
    by_period: dict[str, Decimal] = {}
    for line in lines:
        by_period[line.period] = by_period.get(line.period, ZERO) + Decimal(str(line.difference))
    receipt = _receipt_start(batch)
    by_year = {
        _start_year(label): amount
        for label, amount in relief.allocate_by_period(by_period).items()
    }
    earlier = {year: amount for year, amount in by_year.items() if year < receipt and amount > 0}
    return receipt, dict(sorted(earlier.items()))


async def relief_years(
    session: AsyncSession, user: User, batch_id: uuid.UUID, employee_id: uuid.UUID
) -> dict[str, Any]:
    batch = await _batch(session, user, batch_id)
    receipt, earlier = await _earlier_years(session, user, batch, employee_id)
    return {
        "employee_id": employee_id,
        "receipt_financial_year": short_year(receipt),
        "arrear_amount": sum(earlier.values(), ZERO),
        "years": [
            {"financial_year": short_year(year), "arrear_portion": amount}
            for year, amount in earlier.items()
        ],
    }


async def _tax(
    session: AsyncSession, user: User, financial_year: str, regime: str, taxable: Decimal
) -> Decimal:
    """Tax on a taxable income under the rule set approved for that year."""
    try:
        rules, _ = await tax_service.income_tax_rules(session, user, financial_year)
        tax, rebate, surcharge, cess, _ = computation.compute_annual_tax(
            taxable_income=taxable, regime=regime, rules=rules
        )
    except (tax_service.TaxWorkflowError, computation.TaxRuleError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{financial_year}: {exc}",
        ) from exc
    return max(ZERO, tax - rebate) + surcharge + cess


async def compute_employee_relief(
    session: AsyncSession,
    user: User,
    *,
    batch_id: uuid.UUID,
    employee_id: uuid.UUID,
    regime: str,
    receipt_year_taxable_income: Decimal,
    prior_years: list[dict[str, Any]],
    request: Request | None = None,
) -> SectionRelief:
    """Compute and store the relief for one employee in one batch. Caller commits."""
    batch = await _batch(session, user, batch_id)
    receipt, earlier = await _earlier_years(session, user, batch, employee_id)
    if not earlier:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "No part of this arrear relates to an earlier financial year, so relief "
                "under section 89(1) does not arise."
            ),
        )

    supplied = {_start_year(str(item["financial_year"])): item for item in prior_years}
    missing = [short_year(year) for year in earlier if year not in supplied]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Taxable income is needed for every year the arrear relates to. Missing: "
                + ", ".join(missing)
            ),
        )

    receipt_year = short_year(receipt)
    arrear_amount = sum(earlier.values(), ZERO)

    allocations: list[relief.YearAllocation] = []
    for year, portion in earlier.items():
        item = supplied[year]
        label = short_year(year)
        original = Decimal(str(item["original_taxable_income"]))
        year_regime = str(item.get("regime") or regime)
        allocations.append(
            relief.YearAllocation(
                financial_year=label,
                original_income=original,
                arrear_portion=portion,
                tax_on_original=await _tax(session, user, label, year_regime, original),
                tax_with_arrear=await _tax(
                    session, user, label, year_regime, original + portion
                ),
            )
        )

    try:
        result = relief.compute_relief(
            arrear_amount=arrear_amount,
            receipt_year=receipt_year,
            income_in_receipt_year=receipt_year_taxable_income,
            tax_on_receipt_year_income=await _tax(
                session, user, receipt_year, regime, receipt_year_taxable_income
            ),
            tax_on_receipt_year_income_with_arrear=await _tax(
                session, user, receipt_year, regime, receipt_year_taxable_income + arrear_amount
            ),
            allocations=allocations,
        )
    except relief.ReliefError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    record = await session.scalar(
        tenant_scoped(
            select(SectionRelief).where(
                SectionRelief.employee_id == employee_id,
                SectionRelief.arrear_batch_id == batch.id,
                SectionRelief.financial_year == receipt_year,
            ),
            SectionRelief,
            user,
        )
    )
    if record is None:
        record = SectionRelief(
            tenant_id=user.tenant_id,
            employee_id=employee_id,
            arrear_batch_id=batch.id,
            financial_year=receipt_year,
            arrear_amount=ZERO,
            tax_without_relief=ZERO,
            tax_with_spreading=ZERO,
            relief_amount=ZERO,
        )
        session.add(record)

    record.arrear_amount = result.arrear_amount
    record.tax_without_relief = result.tax_without_relief
    record.tax_with_spreading = result.tax_with_spreading
    record.relief_amount = result.relief_amount
    record.is_beneficial = result.is_beneficial
    record.working = {
        **result.as_working(),
        "regime": regime,
        "receipt_year_taxable_income": str(receipt_year_taxable_income),
        "computed_at": datetime.now(UTC).isoformat(),
    }
    # A recomputed relief makes an earlier Form 10E stale.
    record.form_10e_letter_id = None
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        actor=user,
        action=AuditAction.CREATE,
        entity_type="section_relief",
        entity_id=record.id,
        summary=(
            f"Section 89(1) relief of {result.relief_amount} computed on arrears of "
            f"{result.arrear_amount} received in {receipt_year}"
        ),
        changes={
            "relief_amount": str(result.relief_amount),
            "is_beneficial": result.is_beneficial,
            "years": [a.financial_year for a in allocations],
        },
        request=request,
    )
    return record


async def list_reliefs(
    session: AsyncSession,
    user: User,
    *,
    batch_id: uuid.UUID | None = None,
    employee_id: uuid.UUID | None = None,
) -> list[SectionRelief]:
    stmt = select(SectionRelief).order_by(SectionRelief.created_at.desc())
    if batch_id is not None:
        stmt = stmt.where(SectionRelief.arrear_batch_id == batch_id)
    if employee_id is not None:
        stmt = stmt.where(SectionRelief.employee_id == employee_id)
    return list((await session.scalars(tenant_scoped(stmt, SectionRelief, user))).all())


async def _relief(session: AsyncSession, user: User, relief_id: uuid.UUID) -> SectionRelief:
    record = await session.scalar(
        tenant_scoped(
            select(SectionRelief).where(SectionRelief.id == relief_id), SectionRelief, user
        )
    )
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Relief not found")
    return record


def _money(value: object) -> str:
    from app.services.documents.indian_numbers import group_indian

    return group_indian(Decimal(str(value)).quantize(Decimal("0.01")))


async def issue_form_10e(
    session: AsyncSession, user: User, relief_id: uuid.UUID, *, request: Request | None = None
) -> IssuedLetter:
    """Issue Form 10E for a computed relief. Caller commits."""
    record = await _relief(session, user, relief_id)
    employee = await session.scalar(
        tenant_scoped(select(Employee).where(Employee.id == record.employee_id), Employee, user)
    )
    if employee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")
    if not (employee.pan or "").strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="There is no PAN on the employee record, and Form 10E carries it.",
        )

    working = record.working or {}
    allocations = "; ".join(
        f"{item['financial_year']}: arrear {_money(item['arrear_portion'])}, taxable income "
        f"{_money(item['original_income'])}, tax {_money(item['tax_on_original'])} rising to "
        f"{_money(item['tax_with_arrear'])}"
        for item in working.get("allocations", [])
    )
    template = await support.ensure_letter_template(
        session, user, form_10e.FORM_10E_CODE, form_10e.seed_template
    )
    letter = await documents_service.issue_letter(
        session,
        user,
        employee_id=employee.id,
        template_id=template.id,
        manual_values={
            "employee_pan": employee.pan,
            "receipt_year": record.financial_year,
            "arrear_amount": _money(record.arrear_amount),
            "tax_without_relief": _money(record.tax_without_relief),
            "tax_with_spreading": _money(record.tax_with_spreading),
            "relief_amount": _money(record.relief_amount),
            "allocations": allocations or "No earlier years.",
            "outcome": " ".join(working.get("notes", [])) or "No notes.",
        },
        request=request,
        commit=False,
    )
    record.form_10e_letter_id = letter.id
    await session.flush()
    return letter


async def form_10e_pdf(
    session: AsyncSession, user: User, relief_id: uuid.UUID
) -> tuple[bytes, str]:
    record = await _relief(session, user, relief_id)
    if record.form_10e_letter_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No Form 10E has been issued for this relief.",
        )
    letter, intact = await documents_service.verify_letter(
        session, user, record.form_10e_letter_id
    )
    if not intact:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The stored Form 10E does not match its content hash. Issue it again.",
        )
    data = documents_service.stored_pdf(letter.pdf_storage_key or "")
    if data is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The rendered PDF is not available. Issue Form 10E again.",
        )
    return data, f"{letter.reference_number.replace('/', '_')}.pdf"
