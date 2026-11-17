"""A consultant's payout statement: one PDF per consultant per run.

The weeks paid with their hours and rates, the gross fee, GST, TDS deducted
(section and rate) and any TDS carried to the next payout, the net payable and
the pay date. HR can issue any consultant's; a consultant gets only their own,
once the run is approved.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from html import escape

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.consultant_payouts import ConsultantPayoutLine, PayoutRunStatus
from app.models.contractors import TdsSection
from app.models.org import LegalEntity
from app.models.tenant import Tenant
from app.models.timesheets import Timesheet
from app.models.user import User
from app.services import audit
from app.services.consultants import payouts

__all__ = ["my_payout_statement_pdf", "payout_statement_pdf", "section_name"]

_SECTION_NAME = {
    TdsSection.S194C: "194C (contractors)",
    TdsSection.S194J_PROFESSIONAL: "194J (professional fees)",
    TdsSection.S194J_TECHNICAL: "194J (technical services)",
}
_NOT_FOUND = "Payout statement not found"


def section_name(section: TdsSection) -> str:
    """The TDS section as people read it, e.g. "194J (professional fees)"."""
    return _SECTION_NAME.get(section, section.value)


def _amount(value: object) -> str:
    return f"{Decimal(str(value)):,.2f}"


async def _line_for(
    session: AsyncSession, user: User, line_id: uuid.UUID, *, own: bool = False
) -> ConsultantPayoutLine:
    """The line this caller may read: any for HR, only their own for a consultant
    (and for anybody, HR included, when `own`)."""
    line = await session.scalar(
        tenant_scoped(
            select(ConsultantPayoutLine).where(ConsultantPayoutLine.id == line_id),
            ConsultantPayoutLine,
            user,
        )
    )
    if line is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND)
    if permissions.is_hr(user) and not own:
        return line
    if user.employee_id is None or line.employee_id != user.employee_id:
        raise permissions.forbidden("You can only open your own payout statements.")
    return line


async def my_payout_statement_pdf(
    session: AsyncSession, user: User, line_id: uuid.UUID
) -> tuple[str, bytes]:
    """My pay: the caller's own statement, once the run is approved.

    Own only, HR included: HR opens other people's from Consultant payouts.
    """
    if user.employee_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND)
    return await payout_statement_pdf(session, user, line_id, own=True)


async def payout_statement_pdf(
    session: AsyncSession,
    user: User,
    line_id: uuid.UUID,
    *,
    own: bool = False,
    request: Request | None = None,
) -> tuple[str, bytes]:
    """The statement for one payout line as (file name, PDF bytes).

    A line left out of its run has nothing to state. A consultant sees theirs
    only once the run is approved; so does anybody asking for their `own`.
    """
    from app.services.documents.pdf import Letterhead, render_pdf

    line = await _line_for(session, user, line_id, own=own)
    run = await payouts._run(session, user, line.run_id)
    if line.excluded:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND)
    if (own or not permissions.is_hr(user)) and run.status not in (
        PayoutRunStatus.APPROVED,
        PayoutRunStatus.PAID,
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND)
    employee = await payouts._employee(session, user, line.employee_id)
    if employee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND)

    entity_id = run.legal_entity_id or employee.legal_entity_id
    entity = await session.get(LegalEntity, entity_id) if entity_id else None
    tenant = await session.get(Tenant, user.tenant_id)
    payer = entity.name if entity else (tenant.name if tenant else "Company")

    rows = []
    for week in sorted(await _weeks(session, user, line), key=lambda pair: pair[1].week_start):
        week_row, sheet = week
        rates = sorted(
            {Decimal(b["rate"]) for b in week_row.breakdown or [] if b.get("rate")},
        )
        rows.append(
            "<tr>"
            f"<td>{sheet.week_start.isoformat()}</td>"
            f"<td>{_amount(week_row.hours)}</td>"
            f"<td>{escape(', '.join(_amount(rate) for rate in rates))}</td>"
            f"<td>{_amount(week_row.gross)}</td>"
            "</tr>"
        )
    gst_rate = Decimal(str(line.gst_rate)).normalize()
    tds_rate = Decimal(str(line.tds_rate)).quantize(Decimal("0.001"))
    note = f"<p>{escape(line.tds_note)}</p>" if line.tds_note else ""
    carried = (
        f"<tr><td>TDS carried to next payout</td><td>{_amount(line.tds_carried_out)}</td></tr>\n"
        if Decimal(str(line.tds_carried_out)) > 0
        else ""
    )
    html = f"""
<h2>Payout statement</h2>
<p><b>Paid by:</b> {escape(payer)}<br/>
<b>Consultant:</b> {escape(employee.full_name)} ({escape(employee.employee_code)})
&nbsp; <b>PAN:</b> {escape(employee.pan or "Not provided")}
{f"&nbsp; <b>GSTIN:</b> {escape(employee.gstin)}" if employee.gstin else ""}<br/>
<b>Work up to:</b> {run.cut_off.isoformat()} &nbsp; <b>Pay date:</b> {run.pay_date.isoformat()}</p>
<table class="ctc-table">
<thead><tr><th>Week starting</th><th>Hours</th><th>Rate per hour</th><th>Fee</th></tr></thead>
<tbody>{"".join(rows)}</tbody>
<tfoot><tr><td>Total</td><td>{_amount(line.hours)}</td><td>&nbsp;</td><td>{_amount(line.gross)}</td>
</tr></tfoot>
</table>
<table class="ctc-table">
<tbody>
<tr><td>Fee</td><td>{_amount(line.gross)}</td></tr>
<tr><td>GST at {gst_rate:f}%</td><td>{_amount(line.gst_amount)}</td></tr>
<tr><td>TDS deducted under {_SECTION_NAME[line.section]} at {tds_rate}% on {_amount(line.tds_base)}
</td><td>-{_amount(line.tds_amount)}</td></tr>
{carried}<tr><td><b>Net payable</b></td><td><b>{_amount(line.net_payable)}</b></td></tr>
</tbody>
</table>
{note}
"""
    data = render_pdf(
        merged_html=html,
        reference_number=f"PAYOUT-{run.pay_date:%Y%m%d}-{employee.employee_code}",
        letterhead=Letterhead(company_name=escape(payer)),
    )
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.EXPORT,
        entity_type="consultant_payout_statement",
        entity_id=line.id,
        actor=user,
        changes={"run_id": str(run.id), "employee_id": str(employee.id)},
        summary=f"Issued a payout statement for {employee.full_name}",
        request=request,
    )
    await session.commit()
    safe = "".join(ch for ch in employee.employee_code if ch.isalnum())[:40] or "consultant"
    return f"Payout-{safe}-{run.pay_date.isoformat()}.pdf", data


async def _weeks(session: AsyncSession, user: User, line: ConsultantPayoutLine):
    rows = await payouts._line_weeks(session, user, line.id)
    sheets = {
        sheet.id: sheet
        for sheet in (
            await session.scalars(
                tenant_scoped(
                    select(Timesheet).where(Timesheet.id.in_([w.timesheet_id for w in rows])),
                    Timesheet,
                    user,
                )
            )
        ).all()
    }
    return [(week, sheets[week.timesheet_id]) for week in rows]
