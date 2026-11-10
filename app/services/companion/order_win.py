"""An order win: a signed order opens its positions and writes its prospective P&L.

A manager (or anyone with the rights) tells Ask Mira a customer signed: the PO,
its value, the months, the team with each role's billing and cost rate, and the
recruitment cost per hire. Ask Mira then

1. **checks before it offers anything** — deterministically, here, not in the
   model's head:
   * the deal is Won *and approved* in Saleinx (a rep's Won waits for a
     manager; nothing is opened until it is approved);
   * the PO's value matches what the team bills over the months (within
     half a percent), or it refuses and shows both figures;
   * the same PO was not booked already (one live run per PO number);
   * the person may open jobs in AnytimeHire and write the P&L in AskMira;
2. **offers** the plan: the positions, and the forecast month by month with
   billing, people cost, recruitment and margin;
3. on Confirm, **carries it out step by step** (CompanionRun), recording each:
   open each position in AnytimeHire (each with its own key, "<PO>:<n>", so a
   retried request never doubles it), write the forecast to the finance
   provider, note the Saleinx deal. If a step fails, every step before it is
   undone in reverse — positions withdrawn (and their quota given back), the
   forecast withdrawn — and the person is told exactly what happened.

Money is Decimal throughout; nothing here is a float.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.companion import (
    CompanionAction,
    CompanionActionStatus,
    CompanionRun,
    CompanionRunStatus,
)
from app.models.user import User
from app.services import audit
from app.services.documents.indian_numbers import group_indian
from app.services.finance import ForecastLineIn, ForecastPlan, ForecastWritten, provider_for
from app.services.notifications.dispatch import register_event
from app.services.products import data as products

logger = logging.getLogger("askmira.companion.order_win")

__all__ = [
    "OrderFigures",
    "Role",
    "figures_for",
    "parse_roles",
    "po_mismatch",
    "propose",
    "run",
    "rupees",
]

#: How long a held order (its Won waiting for approval) is kept before it lapses.
HELD_FOR = timedelta(days=14)
#: Once approved, how long the person has to book it before the figures are checked afresh.
READY_FOR = timedelta(hours=24)

ZERO = Decimal(0)
CENT = Decimal("0.01")
#: How far the PO's value may sit from the team's billing before it is refused.
PO_TOLERANCE = Decimal("0.005")
MAX_ROLES = 20
MAX_MONTHS = 60

_LAKH = Decimal(100_000)
_CRORE = Decimal(10_000_000)


# ── Reading what the person said ─────────────────────────────────────────────


def rupees(value: Any) -> Decimal | None:
    """An amount in rupees from what a person writes: 190000, 1,90,000, "1.9 lakh", "Rs 1.2 crore".

    None when empty. A value that is not an amount raises ValueError.
    """
    if value is None:
        return None
    if isinstance(value, int | Decimal):
        return Decimal(value)
    text = str(value).strip().lower()
    if not text:
        return None
    text = re.sub(r"^(?:rs\.?|inr|₹)\s*", "", text).replace(",", "").strip()
    match = re.fullmatch(
        r"(\d+(?:\.\d+)?)\s*(lakhs?|lacs?|l|crores?|cr|k|thousand)?(?:\s*(?:/|per)\s*(?:month|mo|m))?",
        text,
    )
    if not match:
        raise ValueError(f"'{value}' is not an amount. Write it like 190000 or 1.9 lakh.")
    number = Decimal(match.group(1))
    unit = match.group(2) or ""
    if unit.startswith(("lakh", "lac")) or unit == "l":
        number *= _LAKH
    elif unit.startswith(("crore", "cr")):
        number *= _CRORE
    elif unit in ("k", "thousand"):
        number *= 1000
    return number.quantize(CENT, rounding=ROUND_HALF_UP)


def _percent(value: Any, default: Decimal = Decimal(100)) -> Decimal:
    if value in (None, ""):
        return default
    text = str(value).strip().rstrip("%").strip()
    try:
        number = Decimal(text)
    except InvalidOperation:
        raise ValueError(
            f"'{value}' is not an allocation. Write it like 50 for half time."
        ) from None
    if not (ZERO < number <= 100):
        raise ValueError("An allocation is more than 0 and at most 100 (per cent of full time).")
    return number


def _years(value: Any) -> tuple[int, int]:
    """'4 to 6', '4-6', '3 - 5 years', '8+' → (min, max). '8+' reads as 8 to 15."""
    text = str(value or "").strip().lower()
    if not text:
        return 0, 2
    plus = re.fullmatch(r"(\d{1,2})\s*\+\s*(?:years?|yrs?)?", text)
    if plus:
        low = int(plus.group(1))
        return low, min(low + 7, 40)
    span = re.fullmatch(r"(\d{1,2})\s*(?:-|\u2013|to)\s*(\d{1,2})\s*(?:years?|yrs?)?", text)
    if span:
        low, high = int(span.group(1)), int(span.group(2))
        if high < low:
            raise ValueError(f"The experience range '{value}' ends before it starts.")
        return low, high
    one = re.fullmatch(r"(\d{1,2})\s*(?:years?|yrs?)?", text)
    if one:
        return int(one.group(1)), int(one.group(1))
    raise ValueError(f"'{value}' is not an experience range. Write it like 4 to 6, or 8+.")


@dataclass(frozen=True, slots=True)
class Role:
    title: str
    count: int
    experience: tuple[int, int]
    #: Per cent of full time this order pays for (a PM at 50%).
    allocation: Decimal
    #: What the customer is billed for one person a month, at full time.
    bill_rate: Decimal
    #: What one person costs a month (loaded), at full time.
    cost_rate: Decimal
    skills: tuple[str, ...] = ()

    @property
    def share(self) -> Decimal:
        return self.allocation / 100

    @property
    def monthly_billing(self) -> Decimal:
        return (self.bill_rate * self.count * self.share).quantize(CENT, rounding=ROUND_HALF_UP)

    @property
    def monthly_cost(self) -> Decimal:
        return (self.cost_rate * self.count * self.share).quantize(CENT, rounding=ROUND_HALF_UP)

    @property
    def label(self) -> str:
        part = "" if self.allocation == 100 else f" at {_num(self.allocation)}%"
        return f"{self.title} x {self.count}{part}"


def _num(value: Decimal) -> str:
    text = f"{value.normalize():f}"
    return text


def parse_roles(raw: Any) -> list[Role]:
    """The team as the model filled it in, read strictly. A bad row raises ValueError."""
    items = raw if isinstance(raw, list) else []
    roles: list[Role] = []
    for index, item in enumerate(items[:MAX_ROLES], start=1):
        if not isinstance(item, dict):
            continue
        title = " ".join(str(item.get("title") or "").split())[:120]
        if len(title) < 2:
            raise ValueError(f"Role {index} has no title.")
        try:
            count = int(str(item.get("count") or "1").strip())
        except ValueError:
            raise ValueError(f"How many {title}s? '{item.get('count')}' is not a number.") from None
        if not 1 <= count <= 200:
            raise ValueError(f"The number of {title}s must be between 1 and 200.")
        bill = rupees(item.get("bill_rate"))
        cost = rupees(item.get("cost_rate"))
        if bill is None or bill <= 0:
            raise ValueError(f"What is the monthly billing rate for a {title}?")
        if cost is None or cost <= 0:
            raise ValueError(f"What is the monthly loaded cost of a {title}?")
        skills = tuple(
            s.strip()[:60] for s in re.split(r"[,;/]", str(item.get("skills") or "")) if s.strip()
        )[:10]
        roles.append(
            Role(
                title=title,
                count=count,
                experience=_years(item.get("experience")),
                allocation=_percent(item.get("allocation")),
                bill_rate=bill,
                cost_rate=cost,
                skills=skills,
            )
        )
    return roles


@dataclass(frozen=True, slots=True)
class OrderFigures:
    monthly_billing: Decimal
    monthly_cost: Decimal
    months: int
    hires: int
    per_hire: Decimal

    @property
    def billing(self) -> Decimal:
        return self.monthly_billing * self.months

    @property
    def people_cost(self) -> Decimal:
        return self.monthly_cost * self.months

    @property
    def recruitment(self) -> Decimal:
        return self.per_hire * self.hires

    @property
    def cost(self) -> Decimal:
        return self.people_cost + self.recruitment

    @property
    def margin(self) -> Decimal:
        return self.billing - self.cost

    @property
    def margin_pct(self) -> Decimal | None:
        if not self.billing:
            return None
        return (self.margin / self.billing * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def figures_for(roles: list[Role], months: int, per_hire: Decimal) -> OrderFigures:
    return OrderFigures(
        monthly_billing=sum((r.monthly_billing for r in roles), ZERO),
        monthly_cost=sum((r.monthly_cost for r in roles), ZERO),
        months=months,
        # Each person is one hire, whatever share of their time the order pays for.
        hires=sum(r.count for r in roles),
        per_hire=per_hire,
    )


def inr(value: Decimal) -> str:
    """₹71,10,000 — Indian grouping, paise only when there are any."""
    whole = value.quantize(CENT, rounding=ROUND_HALF_UP)
    text = group_indian(int(whole))
    paise = int((whole - int(whole)) * 100)
    return f"₹{text}" + (f".{paise:02d}" if paise else "")


def po_mismatch(po_value: Decimal | None, figures: OrderFigures) -> str | None:
    """Why the PO and the team's billing disagree, or None when they agree (or no PO value)."""
    if po_value is None:
        return None
    gap = abs(po_value - figures.billing)
    if gap <= max(Decimal(1), figures.billing * PO_TOLERANCE):
        return None
    return (
        f"The PO says {inr(po_value)}, but the team bills {inr(figures.monthly_billing)} a month: "
        f"{inr(figures.billing)} over {figures.months} months. The PO is {inr(gap)} "
        f"{'less' if po_value < figures.billing else 'more'} than that. Check the PO value, the "
        "rates or the months; nothing has been opened or written."
    )


def _start(value: Any) -> date:
    try:
        day = date.fromisoformat(str(value or "").strip())
    except ValueError:
        raise ValueError("When does the order start? Give the date as YYYY-MM-DD.") from None
    return day


def _po(value: Any) -> str:
    text = re.sub(r"\s+", "", str(value or "")).upper()[:60]
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9/_.-]{2,59}", text):
        raise ValueError("What is the customer's PO number?")
    return text


def business_key(po_number: str) -> str:
    return f"order_win:{po_number}"


# ── Cards ────────────────────────────────────────────────────────────────────


def _notice(text: str, tone: str = "info") -> dict[str, Any]:
    return {"type": "notice", "tone": tone, "text": text}


def _months_label(start: date, months: int) -> str:
    index = start.year * 12 + start.month - 1 + months - 1
    end = date(index // 12, index % 12 + 1, 1)
    return f"{start:%b %Y} to {end:%b %Y}"


def _run_summary(run: CompanionRun) -> str:
    jobs = [s for s in run.steps if s.get("step") == "job" and s.get("status") == "done"]
    forecast = next(
        (s for s in run.steps if s.get("step") == "forecast" and s.get("status") == "done"), None
    )
    parts = [f"{len(jobs)} position(s) opened in AnytimeHire"]
    raised = [s for s in run.steps if s.get("step") == "requisition" and s.get("status") == "done"]
    if raised:
        parts.append(f"{len(raised)} requisition(s) raised in AskMira")
    if forecast:
        parts.append(f"the prospective P&L written on {forecast.get('account', 'the account')}")
    return ", ".join(parts)


async def deal_now(
    session: AsyncSession, user: User, plan: dict[str, Any]
) -> dict[str, Any] | None:
    """The order's deal as Saleinx shows it to the person now, or None if they can't see it."""
    try:
        found = await products.ask(
            session,
            user,
            "saleinx",
            "deals",
            {"query": str(plan.get("deal_title") or ""), "limit": 10},
        )
    except products.ProductUnavailableError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    for deal in found.get("deals") or []:
        if isinstance(deal, dict) and str(deal.get("id")) == str(plan.get("deal_id")):
            return deal
    return None


# ── Held orders: waiting for the Won's approval, picked up in the background ──

register_event(
    "companion.order.ready",
    "Your order {po_number} is ready to book",
    "{deal_title}'s Won was approved in Saleinx. Your held order {po_number} is ready: open "
    "Ask Mira to book it (positions in AnytimeHire and the prospective P&L).",
)
register_event(
    "companion.order.booked",
    "Order {po_number} was booked",
    "{deal_title}'s Won was approved, so Ask Mira booked your held order {po_number}: {outcome}",
)
register_event(
    "companion.order.withdrawn",
    "Held order {po_number} was withdrawn",
    "{reason} Nothing was opened or written for order {po_number}.",
)

#: The setting a workspace chooses for what happens once a held order's Won is approved.
BOOK_AFTER_APPROVAL = "order_win.book_after_approval"


async def resume_held(session: AsyncSession, actor: User, *, now: datetime) -> dict[str, int]:
    """Look at every held order in the actor's workspace; book or offer the approved ones.

    Each order is checked as the person who asked (Saleinx answers what they may
    see). Approved: the order is ready, offered again for a day and the person
    told; or, when the workspace lets this agent act, booked at once and the
    person told what was done. Rejected (the deal is no longer Won): withdrawn
    and the person told. Still waiting: left as it is.
    """
    from app.services.autonomy import service as autonomy
    from app.services.companion import actions
    from app.services.notifications.dispatch import notify

    held = (
        await session.scalars(
            tenant_scoped(
                select(CompanionAction).where(
                    CompanionAction.kind == "order_win",
                    CompanionAction.status == CompanionActionStatus.PROPOSED,
                    CompanionAction.expires_at > now,
                    CompanionAction.payload.has_key("waiting_for"),
                ),
                CompanionAction,
                actor,
            )
        )
    ).all()
    tally = {"checked": 0, "ready": 0, "booked": 0, "withdrawn": 0, "waiting": 0, "errors": 0}
    if not held:
        return tally
    level, _threshold, _source = await autonomy.effective_autonomy(
        session, actor, action_key=BOOK_AFTER_APPROVAL
    )
    for action in held:
        tally["checked"] += 1
        person = await session.get(User, action.user_id)
        if person is None or not person.is_active:
            continue
        plan = dict(action.payload)
        try:
            deal = await deal_now(session, person, plan)
        except HTTPException:
            tally["errors"] += 1
            continue
        facts = {"po_number": plan.get("po_number"), "deal_title": plan.get("deal_title")}
        if deal is None or deal.get("stage") != "won":
            where = f" (it is at {deal.get('stage_label')})" if deal else ""
            reason = f"{plan.get('deal_title')} is no longer Won in Saleinx{where}."
            action.status = CompanionActionStatus.CANCELLED
            action.decided_at = now
            action.decided_via = "automation"
            action.result = {"text": reason + " The held order was withdrawn.", "error": True}
            _say_in_chat(
                session, action, reason + " I withdrew the held order; nothing was opened."
            )
            await audit.record(
                session,
                tenant_id=action.tenant_id,
                action=AuditAction.UPDATE,
                entity_type="companion_action",
                entity_id=action.id,
                actor_type="system",
                changes={"kind": "order_win", "status": "cancelled", "why": "won_not_approved"},
                summary=f"Held order {plan.get('po_number')} withdrawn: the Won was not approved",
                request=None,
            )
            await session.commit()
            await notify(
                session,
                tenant_id=action.tenant_id,
                event_key="companion.order.withdrawn",
                recipient_user_ids=[person.id],
                payload={**facts, "reason": reason},
            )
            tally["withdrawn"] += 1
            continue
        if deal.get("approval_status") != "approved":
            tally["waiting"] += 1
            continue

        # Approved: the order is no longer held.
        plan.pop("waiting_for", None)
        plan["approved_seen_at"] = now.isoformat()
        action.payload = plan
        summary = dict(action.summary)
        summary.pop("waiting", None)
        summary["notes"] = [
            f"{plan.get('deal_title')}: the Won was approved in Saleinx on {now:%d %b %Y}.",
            *summary.get("notes", []),
        ]
        action.summary = summary
        action.expires_at = now + READY_FOR
        await session.commit()
        if level.applies_automatically:
            action_id = action.id
            done = await actions.confirm(
                session, person, action_id, owner_id=person.id, via="automation", request=None
            )
            outcome = (done.result or {}).get("text") or done.status.value
            _say_in_chat(
                session,
                done,
                f"The Won on {plan.get('deal_title')} was approved, so I booked order "
                f"{plan.get('po_number')}: {outcome}",
            )
            await session.commit()
            await notify(
                session,
                tenant_id=done.tenant_id,
                event_key="companion.order.booked",
                recipient_user_ids=[person.id],
                payload={**facts, "outcome": outcome},
            )
            tally["booked"] += 1
        else:
            _say_in_chat(
                session,
                action,
                f"The Won on {plan.get('deal_title')} was approved. Your held order "
                f"{plan.get('po_number')} is ready: check it and book it here.",
            )
            await session.commit()
            await notify(
                session,
                tenant_id=action.tenant_id,
                event_key="companion.order.ready",
                recipient_user_ids=[person.id],
                payload=facts,
            )
            tally["ready"] += 1
    return tally


def _say_in_chat(session: AsyncSession, action: CompanionAction, text: str) -> None:
    """A note from Ask Mira in the conversation the order came from, with its card as it is now."""
    from app.models.companion import CompanionMessage
    from app.services.companion import actions

    session.add(
        CompanionMessage(
            tenant_id=action.tenant_id,
            conversation_id=action.conversation_id,
            user_id=action.user_id,
            role="assistant",
            text=text,
            cards=[actions.card_for(action)],
        )
    )


async def _live_run(session: AsyncSession, user: User, key: str) -> CompanionRun | None:
    return await session.scalar(
        tenant_scoped(
            select(CompanionRun).where(
                CompanionRun.business_key == key,
                CompanionRun.status.in_(
                    (CompanionRunStatus.RUNNING, CompanionRunStatus.DONE, CompanionRunStatus.STUCK)
                ),
            ),
            CompanionRun,
            user,
        )
    )


def _deal_choice(deals: list[dict[str, Any]], wanted: str) -> dict[str, Any] | list[dict[str, Any]]:
    """The one deal meant: an exact title or company match wins over a partial one."""
    dashes = str.maketrans({0x2013: "-", 0x2014: "-"})
    want = " ".join(wanted.lower().translate(dashes).split())
    exact = [
        d
        for d in deals
        if want in (str(d.get("title") or "").lower(), str(d.get("company") or "").lower())
        or str(d.get("name") or "").lower() == want
    ]
    pool = exact or deals
    return pool[0] if len(pool) == 1 else pool


# ── Propose ──────────────────────────────────────────────────────────────────


async def propose(
    session: AsyncSession, user: User, inputs: dict[str, Any], ctx: Any
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Check an order win end to end and offer it. Nothing changes until the person confirms."""
    from app.services.companion import actions

    try:
        po = _po(inputs.get("po_number"))
        start = _start(inputs.get("start_date"))
        months = int(inputs.get("months") or 0)
        if not 1 <= months <= MAX_MONTHS:
            raise ValueError(f"For how many months? Between 1 and {MAX_MONTHS}.")
        roles = parse_roles(inputs.get("roles"))
        if not roles:
            raise ValueError(
                "Which roles does the order need? Give each one's title, how many, experience, "
                "allocation, monthly billing rate and monthly loaded cost."
            )
        per_hire_raw = inputs.get("recruitment_cost_per_hire")
        per_hire = rupees(per_hire_raw) if str(per_hire_raw or "").strip() else None
        if per_hire is None:
            raise ValueError("What does recruiting one person cost? Say 0 if nothing.")
        po_value = (
            rupees(inputs.get("po_value")) if str(inputs.get("po_value") or "").strip() else None
        )
    except ValueError as exc:
        return [_notice(str(exc), "warning")], {}

    used = {"po_number": po, "start_date": start.isoformat(), "months": months}
    key = business_key(po)
    earlier = await _live_run(session, user, key)
    if earlier is not None:
        when = earlier.created_at.strftime("%d %b %Y") if earlier.created_at else "earlier"
        if earlier.status is CompanionRunStatus.DONE:
            text = (
                f"PO {po} was already booked on {when}: {_run_summary(earlier)}. Nothing was "
                "done again. To change it, withdraw the forecast on the Account P&L page or the "
                "positions in AnytimeHire first."
            )
        elif earlier.status is CompanionRunStatus.RUNNING:
            text = f"PO {po} is being booked right now. Give it a moment and ask again."
        else:
            text = (
                f"PO {po} was booked on {when} but part of it could not be undone after a "
                f"failure: {_run_summary(earlier)}. Someone needs to finish it by hand before "
                "it is tried again."
            )
        card = _notice(text, "warning" if earlier.status is not CompanionRunStatus.DONE else "info")
        card["model_note"] = text
        return [card], used
    # The same order already waiting on the person's say in this conversation: asked
    # again as it was, that offer; asked with a change ("raise them for Delivery"), a
    # new offer that replaces it (actions._offer `replaces`).
    asked = _fingerprint(inputs)
    waiting = await session.scalar(
        tenant_scoped(
            select(CompanionAction).where(
                CompanionAction.kind == "order_win",
                CompanionAction.user_id == user.id,
                CompanionAction.conversation_id == ctx.conversation_id,
                CompanionAction.status == CompanionActionStatus.PROPOSED,
                CompanionAction.expires_at > datetime.now(UTC),
            ),
            CompanionAction,
            user,
        )
    )
    if (
        waiting is not None
        and (waiting.payload or {}).get("po_number") == po
        and (waiting.payload or {}).get("_asked") == asked
    ):
        return [actions.card_for(waiting)], used

    # The deal: Won and approved in Saleinx.
    wanted = " ".join(str(inputs.get("deal") or inputs.get("client") or "").split())
    if not wanted:
        return [_notice("Which customer or deal in Saleinx is this order for?")], used
    if await products.role_in(session, user, "saleinx") is None:
        return [
            _notice(
                "Saleinx is not linked to your workspace for you, so the deal can't be checked.",
                "warning",
            )
        ], used
    try:
        found = await products.ask(
            session, user, "saleinx", "deals", {"query": wanted, "limit": 10}
        )
    except products.ProductUnavailableError as exc:
        return [_notice(str(exc), "warning")], used
    deals = [d for d in found.get("deals") or [] if isinstance(d, dict)]
    if not deals:
        return [_notice(f"No deal you can see in Saleinx matches “{wanted}”.", "warning")], used
    won = [d for d in deals if d.get("stage") == "won"]
    if not won:
        choice = _deal_choice(deals, wanted)
        deal = choice if isinstance(choice, dict) else deals[0]
        return [
            _notice(
                f"{deal.get('title')} is at {deal.get('stage_label')} in Saleinx. An order is "
                "booked once its deal is Won and approved there.",
                "warning",
            )
        ], used
    choice = _deal_choice(won, wanted)
    if isinstance(choice, list):
        names = "; ".join(str(d.get("title")) for d in choice[:6])
        text = f"More than one won deal matches “{wanted}”: {names}. Which one?"
        return [_notice(text)], used
    deal = choice
    # A rep's Won waiting for a manager: everything else is checked now, and the
    # order is held (resume_held) — nothing is opened or written until approval.
    held = deal.get("approval_status") != "approved"

    # Who may do each step.
    hiring_role = await products.role_in(session, user, "interview")
    problems = []
    if hiring_role not in ("admin", "recruiter"):
        problems.append("open positions in AnytimeHire (that is for its admins and recruiters)")
    if not permissions.is_hr(user):
        problems.append("write the P&L (that is for admins and sub-admins with payroll rights)")
    if problems:
        return [
            _notice(
                "You can't " + " or ".join(problems) + ". Ask your HR admin to book this order.",
                "warning",
            )
        ], used

    figures = figures_for(roles, months, per_hire)
    mismatch = po_mismatch(po_value, figures)
    if mismatch:
        card = _notice(mismatch, "warning")
        card["model_note"] = mismatch
        return [card], used

    from app.services.companion import order_requisitions

    department, department_notes, question = await order_requisitions.plan_for(
        session, user, str(inputs.get("department") or ""), roles
    )
    if question is not None:
        return [question], used

    account = " ".join(str(inputs.get("client") or deal.get("company") or deal.get("name")).split())
    location = " ".join(str(inputs.get("location") or "").split())[:120] or "Not stated"
    mode = " ".join(str(inputs.get("work_mode") or "").split())[:40] or "Not stated"
    plan = {
        "_asked": asked,
        "po_number": po,
        "po_value": f"{po_value}" if po_value is not None else None,
        "deal_id": deal.get("id"),
        "deal_title": deal.get("title"),
        "account": account[:160],
        "start_date": start.isoformat(),
        "start_month": start.replace(day=1).isoformat(),
        "months": months,
        "location": location,
        "work_mode": mode,
        "per_hire": f"{per_hire}",
        "department": department,
        "roles": [
            {
                "title": r.title,
                "count": r.count,
                "experience": list(r.experience),
                "allocation": f"{r.allocation}",
                "bill_rate": f"{r.bill_rate}",
                "cost_rate": f"{r.cost_rate}",
                "skills": list(r.skills),
            }
            for r in roles
        ],
        "figures": {
            "monthly_billing": f"{figures.monthly_billing}",
            "monthly_cost": f"{figures.monthly_cost}",
            "billing": f"{figures.billing}",
            "people_cost": f"{figures.people_cost}",
            "recruitment": f"{figures.recruitment}",
            "cost": f"{figures.cost}",
            "margin": f"{figures.margin}",
            "margin_pct": f"{figures.margin_pct}" if figures.margin_pct is not None else None,
            "hires": figures.hires,
        },
    }
    span = _months_label(start.replace(day=1), months)
    notes = [
        (
            f"Positions open in AnytimeHire as {location}, {mode}, each once its requisition "
            "is approved: "
            if department
            else f"Positions open in AnytimeHire as {location}, {mode}: "
        )
        + "; ".join(
            f"{r.title} ({r.count} opening{'s' if r.count > 1 else ''}, "
            f"{r.experience[0]} to {r.experience[1]} years)"
            for r in roles
        ),
        f"The prospective P&L goes on the account “{account}” for {span}; recruitment "
        f"({figures.hires} hires at {inr(per_hire)} each) in the first month.",
    ]
    notes.extend(department_notes)
    if po_value is None:
        notes.append(
            f"No PO value was given: the P&L uses the team's billing, {inr(figures.billing)}."
        )
    else:
        notes.append(f"The PO's {inr(po_value)} matches the team's billing.")
    notes.append(
        "If any step fails, the steps before it are undone: "
        + ("requisitions cancelled, " if department else "positions withdrawn, ")
        + "the forecast taken back."
    )
    waiting = None
    if held:
        waiting = (
            f"Held: {deal.get('title')} is Won but waits for a manager's approval in Saleinx. "
            "Nothing is opened or written until it is approved. Ask Mira checks Saleinx every "
            "15 minutes and tells you the moment it is approved, so you can book it then."
        )
        plan["waiting_for"] = {
            "event": "won_approved",
            "deal_id": deal.get("id"),
            "deal_title": deal.get("title"),
            "since": datetime.now(UTC).isoformat(),
        }
    action = await actions._offer(
        session,
        user,
        ctx,
        kind="order_win",
        replaces=f"po:{key}",
        payload=plan,
        summary={
            "title": f"Book PO {po}: {deal.get('title')}",
            "figures": [
                {"label": "Deal", "value": str(deal.get("title"))},
                {"label": "Starts", "value": f"{start:%d %b %Y}, {months} months"},
                {"label": "Billing a month", "value": inr(figures.monthly_billing)},
                {"label": "Cost a month", "value": inr(figures.monthly_cost)},
                {"label": "Billing in all", "value": inr(figures.billing)},
                {"label": "People cost", "value": inr(figures.people_cost)},
                {"label": "Recruitment", "value": inr(figures.recruitment)},
                {
                    "label": "Margin",
                    "value": f"{inr(figures.margin)} ({figures.margin_pct}%)",
                },
            ],
            "table": {
                "columns": ["Role", "People", "Allocation", "Bill / month", "Cost / month"],
                "rows": [
                    [
                        r.title,
                        r.count,
                        f"{_num(r.allocation)}%",
                        inr(r.monthly_billing),
                        inr(r.monthly_cost),
                    ]
                    for r in roles
                ],
            },
            "notes": notes,
            "confirm_label": "Open positions and write the P&L",
            "question": "Book this order?",
            **({"waiting": waiting} if waiting else {}),
        },
    )
    if waiting:
        action.expires_at = datetime.now(UTC) + HELD_FOR
        card = actions.card_for(action)
        card["model_note"] = (
            waiting + " Say it is held and who must approve it; never say it is booked or done."
        )
        return [card], used
    return [actions.card_for(action)], used


# ── Carry it out ─────────────────────────────────────────────────────────────


def _step(run: CompanionRun, entry: dict[str, Any]) -> None:
    entry = {**entry, "at": datetime.now(UTC).isoformat()}
    # A new list, so the JSONB column sees the change.
    run.steps = [*run.steps, entry]


async def _record(
    session: AsyncSession, user: User, run: CompanionRun, summary: str, request: Request | None
) -> None:
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="companion_run",
        entity_id=run.id,
        actor=user,
        changes={"status": run.status.value, "steps": run.steps[-1:] if run.steps else []},
        summary=summary,
        request=request,
    )


def _fingerprint(inputs: dict[str, Any]) -> str:
    """What the person asked for, to tell the same request again from a changed one."""
    keys = ("po_number", "po_value", "start_date", "months", "roles", "department",
            "recruitment_cost_per_hire", "deal", "client", "location", "work_mode")  # fmt: skip
    asked = {key: inputs.get(key) for key in keys}
    return hashlib.sha256(json.dumps(asked, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _job_params(plan: dict[str, Any], index: int, role: dict[str, Any]) -> dict[str, Any]:
    start = date.fromisoformat(plan["start_date"])
    allocation = Decimal(role["allocation"])
    share = "" if allocation == 100 else f", {_num(allocation)}% allocation"
    return {
        "key": f"{plan['po_number']}:{index}",
        "title": f"{role['title']} ({plan['account']})"[:255],
        "department": "Delivery",
        "location": plan["location"],
        "job_type": "Full Time",
        "emp_type": plan["work_mode"],
        "experience_min": role["experience"][0],
        "experience_max": role["experience"][1],
        "openings": role["count"],
        "skills": [{"name": s} for s in role.get("skills") or []],
        "responsibilities": [
            f"{plan['account']}: {plan['months']}-month engagement from {start:%d %b %Y} "
            f"(PO {plan['po_number']}){share}."
        ],
    }


def _forecast_plan(plan: dict[str, Any], run_id: uuid.UUID) -> ForecastPlan:
    months = int(plan["months"])
    lines: list[ForecastLineIn] = []
    for role in plan["roles"]:
        r = Role(
            title=role["title"],
            count=int(role["count"]),
            experience=tuple(role["experience"]),  # type: ignore[arg-type]
            allocation=Decimal(role["allocation"]),
            bill_rate=Decimal(role["bill_rate"]),
            cost_rate=Decimal(role["cost_rate"]),
        )
        lines.append(ForecastLineIn("billing", r.label, r.monthly_billing, 0, months))
        lines.append(ForecastLineIn("people_cost", r.label, r.monthly_cost, 0, months))
    recruitment = Decimal(plan["figures"]["recruitment"])
    if recruitment > 0:
        lines.append(
            ForecastLineIn(
                "recruitment",
                f"Recruitment ({plan['figures']['hires']} hires)",
                recruitment,
                0,
                1,
            )
        )
    return ForecastPlan(
        account_name=plan["account"],
        reference=plan["po_number"],
        title=f"PO {plan['po_number']}: {plan['deal_title']}"[:200],
        start_month=date.fromisoformat(plan["start_month"]),
        months=months,
        lines=lines,
        order_value=Decimal(plan["po_value"]) if plan.get("po_value") else None,
        source="order_win",
        source_ref=str(run_id),
        notes=f"Booked from Ask Mira. Saleinx deal {plan.get('deal_id')}.",
    )


async def _undo(
    session: AsyncSession, user: User, run: CompanionRun, request: Request | None
) -> list[str]:
    """Undo every done step, newest first. Returns what could not be undone."""
    stuck: list[str] = []
    for entry in reversed([s for s in run.steps if s.get("status") == "done"]):
        if entry.get("step") == "forecast":
            try:
                await provider_for(session, user).withdraw_forecast(
                    session,
                    user,
                    ForecastWritten(
                        forecast_id=uuid.UUID(entry["forecast_id"]),
                        account_id=uuid.UUID(entry["account_id"]),
                        account_code=entry.get("account_code", ""),
                        account_name=entry.get("account", ""),
                        account_created=bool(entry.get("account_created")),
                    ),
                    reason=f"PO {run.plan['po_number']}: a later step failed",
                    request=request,
                )
                _step(
                    run,
                    {"step": "undo_forecast", "status": "done", "label": "P&L forecast withdrawn"},
                )
            except HTTPException as exc:
                stuck.append(f"the P&L forecast ({exc.detail})")
                _step(run, {"step": "undo_forecast", "status": "failed", "detail": str(exc.detail)})
        elif entry.get("step") == "requisition":
            from app.services.companion import order_requisitions

            savepoint = await session.begin_nested()
            try:
                await order_requisitions.cancel(
                    session,
                    user,
                    entry["requisition_id"],
                    f"Order {run.plan['po_number']}: a later step failed",
                    request,
                )
                _step(
                    run,
                    {
                        "step": "undo_requisition",
                        "status": "done",
                        "label": f"Requisition {entry.get('code')} cancelled",
                    },
                )
            except (HTTPException, ValueError, SQLAlchemyError) as exc:
                await savepoint.rollback()
                detail = exc.detail if isinstance(exc, HTTPException) else "it could not be changed"
                stuck.append(f"the requisition {entry.get('code')} ({detail})")
                _step(run, {"step": "undo_requisition", "status": "failed", "detail": str(detail)})
        elif entry.get("step") == "job" and not entry.get("existing"):
            try:
                answer = await products.act(
                    session, user, "interview", "job.withdraw", {"key": entry["key"]}
                )
                _step(
                    run,
                    {
                        "step": "undo_job",
                        "status": "done",
                        "key": entry["key"],
                        "label": f"{entry.get('title')} withdrawn",
                        "withdrawn": bool(answer.get("withdrawn")),
                    },
                )
            except products.ProductUnavailableError as exc:
                stuck.append(f"the position “{entry.get('title')}” in AnytimeHire ({exc})")
                _step(
                    run,
                    {
                        "step": "undo_job",
                        "status": "failed",
                        "key": entry["key"],
                        "detail": str(exc),
                    },
                )
        await session.commit()
    return stuck


async def run(
    session: AsyncSession, user: User, action: CompanionAction, request: Request | None
) -> dict[str, Any]:
    """Book the order the person confirmed, step by step, undoing on failure."""
    plan = dict(action.payload)
    if plan.get("waiting_for"):
        raise HTTPException(
            status_code=409,
            detail="This order is held until a manager approves the Won in Saleinx.",
        )
    # The Won is checked again now: approval can be taken back after the offer.
    now_deal = await deal_now(session, user, plan)
    if (
        now_deal is None
        or now_deal.get("stage") != "won"
        or (now_deal.get("approval_status") != "approved")
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                f"{plan.get('deal_title')} is not Won and approved in Saleinx now, so nothing "
                "was opened or written."
            ),
        )
    key = business_key(plan["po_number"])
    if await _live_run(session, user, key) is not None:
        raise HTTPException(
            status_code=409,
            detail=f"PO {plan['po_number']} was already booked. Nothing was done again.",
        )
    booking = CompanionRun(
        tenant_id=user.tenant_id,
        kind="order_win",
        business_key=key,
        user_id=user.id,
        action_id=action.id,
        status=CompanionRunStatus.RUNNING,
        plan=plan,
        steps=[],
    )
    session.add(booking)
    try:
        await session.flush()
    except IntegrityError:
        raise HTTPException(
            status_code=409, detail=f"PO {plan['po_number']} is already being booked."
        ) from None
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="companion_run",
        entity_id=booking.id,
        actor=user,
        changes={"kind": "order_win", "po_number": plan["po_number"], "figures": plan["figures"]},
        summary=f"Ask Mira started booking PO {plan['po_number']} for {plan['account']}",
        request=request,
    )
    await session.commit()
    run_id = booking.id

    failure: str | None = None
    # 1. Each role. With a delivery department: a requisition there, sent for approval,
    #    whose position opens in AnytimeHire once it is approved (hiring/linked_jobs.py),
    #    so nobody recruits for a role its approvers turned down. Without one, the
    #    position opens at once.
    for index, role in enumerate(plan["roles"], start=1):
        if plan.get("department"):
            from app.services.companion import order_requisitions

            savepoint = await session.begin_nested()
            try:
                raised = await order_requisitions.raise_for_role(
                    session, user, plan, role, None, request
                )
            except (HTTPException, ValueError, SQLAlchemyError) as exc:
                # A refusal, a value a check refused, or the database (two codes at once):
                # this step is rolled back, said, and everything before it undone.
                if not isinstance(exc, HTTPException):
                    logger.exception("Order %s: a requisition step failed", plan["po_number"])
                await savepoint.rollback()
                detail = (
                    str(exc.detail)
                    if isinstance(exc, HTTPException)
                    else "it did not pass a check. Try again in a moment."
                )
                booking = await session.get(CompanionRun, run_id) or booking
                failure = f"The requisition for “{role['title']}” could not be raised: {detail}"
                _step(booking, {"step": "requisition", "status": "failed", "detail": detail})
                await session.commit()
                break
            booking = await session.get(CompanionRun, run_id) or booking
            _step(
                booking, {"step": "requisition", "status": "done", "title": role["title"], **raised}
            )
            await _record(
                session,
                user,
                booking,
                f"Order {plan['po_number']}: requisition {raised['code']} raised for "
                f"{role['title']}",
                request,
            )
            await session.commit()
            continue
        params = _job_params(plan, index, role)
        try:
            answer = await products.act(session, user, "interview", "job.open", params)
        except products.ProductUnavailableError as exc:
            failure = f"AnytimeHire refused the position “{role['title']}”: {exc}"
            _step(
                booking,
                {"step": "job", "status": "failed", "key": params["key"], "detail": str(exc)},
            )
            await session.commit()
            break
        job = answer.get("job") if isinstance(answer.get("job"), dict) else {}
        _step(
            booking,
            {
                "step": "job",
                "status": "done",
                "key": params["key"],
                "ref": job.get("ref"),
                "title": job.get("title") or params["title"],
                "openings": job.get("openings") or role["count"],
                "existing": bool(answer.get("existing")),
            },
        )
        await _record(
            session,
            user,
            booking,
            f"Order {plan['po_number']}: opened {params['title']} in AnytimeHire",
            request,
        )
        await session.commit()

    # 2. The prospective P&L.
    if failure is None:
        try:
            written = await provider_for(session, user).write_forecast(
                session, user, _forecast_plan(plan, run_id), request=request
            )
            booking = await session.get(CompanionRun, run_id) or booking
            _step(
                booking,
                {
                    "step": "forecast",
                    "status": "done",
                    "forecast_id": str(written.forecast_id),
                    "account_id": str(written.account_id),
                    "account_code": written.account_code,
                    "account": written.account_name,
                    "account_created": written.account_created,
                    "existing": written.existing,
                },
            )
            await _record(
                session,
                user,
                booking,
                f"Order {plan['po_number']}: prospective P&L written",
                request,
            )
            await session.commit()
        except HTTPException as exc:
            await session.rollback()
            booking = await session.get(CompanionRun, run_id) or booking
            failure = f"The P&L could not be written: {exc.detail}"
            _step(booking, {"step": "forecast", "status": "failed", "detail": str(exc.detail)})
            await session.commit()

    if failure is not None:
        booking = await session.get(CompanionRun, run_id) or booking
        stuck = await _undo(session, user, booking, request)
        booking.status = CompanionRunStatus.STUCK if stuck else CompanionRunStatus.UNDONE
        booking.finished_at = datetime.now(UTC)
        await _record(
            session,
            user,
            booking,
            f"Order {plan['po_number']} not booked: {failure}"
            + (
                f"; could not undo {', '.join(stuck)}" if stuck else "; everything before it undone"
            ),
            request,
        )
        await session.commit()
        if stuck:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{failure.rstrip('.')}. The steps before it were undone except "
                    f"{', '.join(stuck)}: someone needs to remove that by hand."
                ),
            )
        opened = [s for s in booking.steps if s.get("step") == "job" and s.get("status") == "done"]
        undone = f" The {len(opened)} position(s) already opened were withdrawn." if opened else ""
        raise HTTPException(
            status_code=409,
            detail=f"{failure.rstrip('.')}.{undone} Nothing from this order was kept.",
        )

    # 3. A note on the deal (a courtesy: its failure is said, not undone).
    jobs = [s for s in booking.steps if s.get("step") == "job" and s.get("status") == "done"]
    asked_for = [
        s for s in booking.steps if s.get("step") == "requisition" and s.get("status") == "done"
    ]
    figures = plan["figures"]
    staffed = (
        f"{len(asked_for)} requisition(s) raised for {plan['department']['name']}; each opens "
        f"in AnytimeHire once approved ({figures['hires']} hires)"
        if plan.get("department")
        else f"{len(jobs)} position(s) opened in AnytimeHire ({figures['hires']} hires)"
    )
    note = (
        f"Order booked from AskMira: PO {plan['po_number']}, {plan['months']} months from "
        f"{date.fromisoformat(plan['start_date']):%d %b %Y}. {staffed}. Prospective P&L: billing "
        f"{inr(Decimal(figures['billing']))}, cost {inr(Decimal(figures['cost']))}, margin "
        f"{inr(Decimal(figures['margin']))} ({figures['margin_pct']}%)."
    )
    warning = None
    try:
        await products.act(
            session, user, "saleinx", "deal.note", {"deal_id": plan["deal_id"], "body": note}
        )
        _step(
            booking,
            {"step": "deal_note", "status": "done", "label": "Note added on the Saleinx deal"},
        )
    except products.ProductUnavailableError as exc:
        warning = f"The note on the Saleinx deal could not be added ({exc})."
        _step(booking, {"step": "deal_note", "status": "failed", "detail": str(exc)})
    booking.status = CompanionRunStatus.DONE
    booking.finished_at = datetime.now(UTC)
    await _record(
        session,
        user,
        booking,
        f"Order {plan['po_number']} booked: {_run_summary(booking)}",
        request,
    )
    await session.commit()

    forecast = next(s for s in booking.steps if s.get("step") == "forecast")
    pnl = (
        f"the prospective P&L is on “{forecast.get('account')}”: "
        f"billing {inr(Decimal(figures['billing']))}, cost {inr(Decimal(figures['cost']))}, margin "
        f"{inr(Decimal(figures['margin']))} ({figures['margin_pct']}%)."
    )
    if asked_for:
        codes = ", ".join(str(s.get("code")) for s in asked_for)
        sent = all(s.get("submitted") for s in asked_for)
        text = (
            f"PO {plan['po_number']} is booked: {pnl} Requisitions "
            f"{codes} are raised for {plan['department']['name']}"
            + (
                " and waiting for approval; each opens its position in AnytimeHire once approved."
                if sent
                else " as drafts: send them for approval in Hiring, and each opens its "
                "position in AnytimeHire once approved."
            )
        )
        flagged = [s for s in asked_for if s.get("told")]
        if flagged:
            text += " The approvers are told: " + " ".join(dict.fromkeys(
                str(s["told"]) for s in flagged
            ))  # fmt: skip
    else:
        text = (
            f"PO {plan['po_number']} is booked. {len(jobs)} position(s) are open in AnytimeHire "
            f"for {figures['hires']} hires, and {pnl}"
        )
    if warning:
        text += " " + warning
    return {
        "text": text,
        "open": "/payroll/accounts",
        "reference": str(run_id),
        "positions": [{"title": s.get("title"), "ref": s.get("ref")} for s in jobs],
    }
