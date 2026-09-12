"""Prove the in-tenant access rules against the real database, without changing it.

    cd backend
    .venv/Scripts/python.exe scripts/verify_access.py

Needs the demo company from `scripts/seed_demo.py`.

Everything — the test records it creates and every request it makes — runs inside
ONE database transaction that is rolled back at the end, whatever happens. The
integration suite cannot be pointed at a shared database because it drops the
schema between tests; this can, because it never commits.

Each check signs in as one of the six demo roles, calls the real app, and
compares the outcome with what the rule says should happen. A 422 is reported as
inconclusive rather than as a pass: it means the request was refused for its
shape before the permission check was ever reached.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import enum
import inspect
import re
import sys
import types
import typing
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from fastapi.routing import APIRoute
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.core.security import create_access_token
from app.db.session import get_session
from app.main import app
from app.models.base import Base
from app.models.employee import Employee
from app.models.tenant import Tenant
from app.models.user import User

DEMO_SLUG = "askmira-demo"
FY = "2026-27"
TODAY = dt.date.today()


def model_for(table: str):
    for mapper in Base.registry.mappers:
        if mapper.local_table is not None and mapper.local_table.name == table:
            return mapper.class_
    raise LookupError(table)


LeaveType = model_for("leave_types")
LeaveRequest = model_for("leave_requests")
Requisition = model_for("requisitions")
Candidate = model_for("candidates")
TaxDeclaration = model_for("tax_declarations")
Pip = model_for("pips")
Shoutout = model_for("shoutouts")
LetterTemplate = model_for("letter_templates")
IssuedLetter = model_for("issued_letters")


def enum_member(model, column: str, value: str | None = None):
    enum_class = model.__table__.c[column].type.enum_class
    if value is None:
        return next(iter(enum_class))
    return next(m for m in enum_class if m.value == value or m.name == value)


# ── Request bodies built from the real schemas ───────────────────────────────


def sample(annotation):
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin is typing.Annotated:
        return sample(args[0])
    if origin in (typing.Union, types.UnionType):
        return sample(next(a for a in args if a is not type(None)))
    if origin is typing.Literal:
        return args[0]
    if origin in (list, set, tuple, frozenset):
        # A list of objects gets one valid element: a schema that requires at
        # least one item would otherwise refuse the request for its shape before
        # the permission check is ever reached.
        inner = args[0] if args else None
        if inspect.isclass(inner) and issubclass(inner, BaseModel):
            return [build_body(inner, {})]
        return []
    if origin is dict:
        return {}
    if inspect.isclass(annotation):
        if issubclass(annotation, BaseModel):
            return build_body(annotation, {})
        if issubclass(annotation, enum.Enum):
            return next(iter(annotation)).value
        if annotation is uuid.UUID:
            return str(uuid.uuid4())
        if annotation is bool:
            return False
        if annotation is int:
            return 1
        if annotation in (float, Decimal):
            return "1"
        if annotation is dt.datetime:
            return dt.datetime.now(dt.UTC).isoformat()
        if annotation is dt.date:
            return TODAY.isoformat()
        if annotation is str:
            return "verification check"
    return None


def build_body(model: type[BaseModel], overrides: dict) -> dict:
    body = {}
    for name, field in model.model_fields.items():
        if name in overrides:
            body[name] = overrides[name]
        elif field.is_required():
            body[name] = sample(field.annotation)
    return body


def _shape(path: str) -> str:
    """A route template with its placeholder names removed, so `{id}` matches
    `{declaration_id}`."""
    return re.sub(r"\{[^}]+\}", "{}", path)


ROUTES = {
    (method, _shape(route.path)): route
    for route in app.routes
    if isinstance(route, APIRoute)
    for method in route.methods
}


def body_model(method: str, template: str) -> type[BaseModel] | None:
    route = ROUTES[(method, _shape("/api/v1" + template))]
    for annotation in typing.get_type_hints(route.endpoint).values():
        if inspect.isclass(annotation) and issubclass(annotation, BaseModel):
            return annotation
    return None


# ── Checks ───────────────────────────────────────────────────────────────────


@dataclass
class Check:
    label: str
    role: str
    method: str
    template: str
    path_values: dict | None = None
    params: dict | None = None
    body: dict | None = None
    expect: str = "deny"  # "deny" (403), "allow" (not 403), or "ok" (200 + predicate)
    predicate: Callable[[typing.Any], bool] | None = None


async def main() -> int:
    target = make_url(settings.DATABASE_URL)
    print(f"Verifying against {target.host}:{target.port}/{target.database}")
    print("Everything below runs in one transaction and is rolled back.\n")

    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    results: list[tuple[str, str, str, str]] = []
    async with engine.connect() as conn:
        outer = await conn.begin()
        session = AsyncSession(
            bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )

        async def override_session():
            yield session

        app.dependency_overrides[get_session] = override_session
        try:
            tenant = await session.scalar(select(Tenant).where(Tenant.slug == DEMO_SLUG))
            if tenant is None:
                print("The demo company is missing. Run scripts/seed_demo.py first.")
                return 2
            users = {
                u.email.split("@")[0]: u
                for u in (
                    await session.scalars(select(User).where(User.tenant_id == tenant.id))
                ).all()
            }
            staff = {
                e.employee_code: e
                for e in (
                    await session.scalars(select(Employee).where(Employee.tenant_id == tenant.id))
                ).all()
            }
            ceo, priya, karthik, vikram, meera = (
                staff["DEMO001"],
                staff["DEMO002"],
                staff["DEMO003"],
                staff["DEMO004"],
                staff["DEMO005"],
            )
            role_user = {
                "platform_admin": users["platform"],
                "tenant_admin": users["admin"],
                "hr_admin": users["hr.admin"],
                "hr_ops": users["hr.ops"],
                "manager": users["manager"],
                "employee": users["employee"],
            }
            tokens = {
                role: create_access_token(
                    user_id=str(u.id), tenant_id=str(u.tenant_id), role=u.role.value
                )
                for role, u in role_user.items()
            }

            # ── Test records, inside the transaction ────────────────────────
            ceo.date_of_birth, ceo.phone = dt.date(1980, 5, 1), "+919800000001"
            meera.date_of_birth, meera.phone = dt.date(1996, 8, 9), "+919800000005"

            leave_type = LeaveType(tenant_id=tenant.id, name="Sick leave", code="VERIFY_SL")
            session.add(leave_type)
            await session.flush()
            for person, reason in ((ceo, "VERIFY medical appointment"), (meera, "VERIFY personal")):
                session.add(
                    LeaveRequest(
                        tenant_id=tenant.id,
                        employee_id=person.id,
                        leave_type_id=leave_type.id,
                        start_date=TODAY,
                        end_date=TODAY,
                        total_days=Decimal("1"),
                        reason=reason,
                    )
                )

            req_mine = Requisition(
                tenant_id=tenant.id,
                code="VERIFY-R1",
                title="Backend engineer",
                hiring_manager_id=vikram.id,
            )
            req_other = Requisition(tenant_id=tenant.id, code="VERIFY-R2", title="HR partner")
            session.add_all([req_mine, req_other])
            await session.flush()
            session.add_all(
                [
                    Candidate(
                        tenant_id=tenant.id,
                        requisition_id=req_mine.id,
                        full_name="VERIFY Candidate One",
                        email="one@candidate.example",
                    ),
                    Candidate(
                        tenant_id=tenant.id,
                        requisition_id=req_other.id,
                        full_name="VERIFY Candidate Two",
                        email="two@candidate.example",
                    ),
                ]
            )

            ceo_declaration = TaxDeclaration(
                tenant_id=tenant.id,
                employee_id=ceo.id,
                financial_year=FY,
                section="80C",
                sub_category="ppf",
                declared_amount=Decimal("1000"),
            )
            session.add(ceo_declaration)

            pip = Pip(
                tenant_id=tenant.id,
                employee_id=meera.id,
                start_date=TODAY,
                end_date=TODAY + dt.timedelta(days=60),
                reason="VERIFY",
                expectations="VERIFY",
                support_provided="VERIFY",
                initiated_by_user_id=role_user["hr_admin"].id,
            )
            session.add(pip)

            for sender, recipient, visibility, message in (
                (ceo, priya, "private", "VERIFY-PRIVATE"),
                (priya, meera, "company", "VERIFY-COMPANY"),
                (ceo, karthik, "department", "VERIFY-DEPARTMENT"),
            ):
                session.add(
                    Shoutout(
                        tenant_id=tenant.id,
                        from_employee_id=sender.id,
                        to_employee_id=recipient.id,
                        message=message,
                        visibility=enum_member(Shoutout, "visibility", visibility),
                    )
                )

            template = LetterTemplate(
                tenant_id=tenant.id,
                code="VERIFY_OFFER",
                name="Offer",
                category=enum_member(LetterTemplate, "category", "offer"),
                body_html="<p>Offer</p>",
            )
            session.add(template)
            await session.flush()
            ceo_letter = IssuedLetter(
                tenant_id=tenant.id,
                template_id=template.id,
                template_version=1,
                category=enum_member(IssuedLetter, "category", "offer"),
                reference_number="VERIFY/1",
                merged_html="<p>CTC 90,00,000</p>",
                content_hash="verify",
                employee_id=ceo.id,
            )
            session.add(ceo_letter)
            await session.commit()  # a savepoint release; the outer transaction stays open

            # ── What should happen ──────────────────────────────────────────
            def rows(body):
                return body.get("items", body) if isinstance(body, dict) else body

            def messages(body):
                return {r["message"] for r in rows(body)}

            random_id = str(uuid.uuid4())
            checks = [
                # Profiles
                Check(
                    "Colleague's profile hides personal fields",
                    "employee",
                    "GET",
                    "/employees/{id}",
                    {"id": ceo.id},
                    expect="ok",
                    predicate=lambda b: b["date_of_birth"] is None and b["phone"] is None,
                ),
                Check(
                    "Own profile shows personal fields",
                    "employee",
                    "GET",
                    "/employees/{id}",
                    {"id": meera.id},
                    expect="ok",
                    predicate=lambda b: b["date_of_birth"] is not None,
                ),
                Check(
                    "HR sees a colleague's personal fields",
                    "hr_ops",
                    "GET",
                    "/employees/{id}",
                    {"id": ceo.id},
                    expect="ok",
                    predicate=lambda b: b["phone"] == "+919800000001",
                ),
                Check(
                    "Manager sees report's grade, not their birthday",
                    "manager",
                    "GET",
                    "/employees/{id}",
                    {"id": meera.id},
                    expect="ok",
                    predicate=lambda b: b["grade"] == "P3" and b["date_of_birth"] is None,
                ),
                # Leave
                Check(
                    "Leave list is only your own",
                    "employee",
                    "GET",
                    "/leave/requests",
                    expect="ok",
                    predicate=lambda b: {r["employee_id"] for r in rows(b)} == {str(meera.id)},
                ),
                Check(
                    "Colleague's leave refused",
                    "employee",
                    "GET",
                    "/leave/requests",
                    params={"employee_id": str(ceo.id)},
                ),
                Check(
                    "Manager sees their report's leave, not the CEO's",
                    "manager",
                    "GET",
                    "/leave/requests",
                    expect="ok",
                    predicate=lambda b: str(ceo.id) not in {r["employee_id"] for r in rows(b)}
                    and str(meera.id) in {r["employee_id"] for r in rows(b)},
                ),
                Check(
                    "HR sees everybody's leave",
                    "hr_ops",
                    "GET",
                    "/leave/requests",
                    expect="ok",
                    predicate=lambda b: str(ceo.id) in {r["employee_id"] for r in rows(b)},
                ),
                # Letters
                Check(
                    "Letter list excludes the CEO's offer",
                    "employee",
                    "GET",
                    "/letters",
                    expect="ok",
                    predicate=lambda b: all(r["employee_id"] != str(ceo.id) for r in b),
                ),
                Check(
                    "Opening the CEO's offer letter refused",
                    "employee",
                    "GET",
                    "/letters/{id}/verify",
                    {"id": ceo_letter.id},
                ),
                Check(
                    "HR can open the CEO's offer letter",
                    "hr_admin",
                    "GET",
                    "/letters/{id}/verify",
                    {"id": ceo_letter.id},
                    expect="allow",
                ),
                # Candidates
                Check(
                    "Employee refused the candidate list", "employee", "GET", "/hiring/candidates"
                ),
                Check(
                    "Hiring manager sees only their requisition",
                    "manager",
                    "GET",
                    "/hiring/candidates",
                    expect="ok",
                    predicate=lambda b: {r["full_name"] for r in rows(b)}
                    == {"VERIFY Candidate One"},
                ),
                Check(
                    "HR sees every candidate",
                    "hr_ops",
                    "GET",
                    "/hiring/candidates",
                    expect="ok",
                    predicate=lambda b: {"VERIFY Candidate One", "VERIFY Candidate Two"}
                    <= {r["full_name"] for r in rows(b)},
                ),
                # Shoutouts
                Check(
                    "Private and other-department shoutouts hidden",
                    "employee",
                    "GET",
                    "/culture/shoutouts",
                    expect="ok",
                    predicate=lambda b: "VERIFY-COMPANY" in messages(b)
                    and not {"VERIFY-PRIVATE", "VERIFY-DEPARTMENT"} & messages(b),
                ),
                Check(
                    "Recipient sees their private shoutout",
                    "hr_admin",
                    "GET",
                    "/culture/shoutouts",
                    expect="ok",
                    predicate=lambda b: "VERIFY-PRIVATE" in messages(b),
                ),
                Check(
                    "HR ops sees department shoutouts, not others' private",
                    "hr_ops",
                    "GET",
                    "/culture/shoutouts",
                    expect="ok",
                    predicate=lambda b: "VERIFY-DEPARTMENT" in messages(b)
                    and "VERIFY-PRIVATE" not in messages(b),
                ),
                # Rewards
                Check(
                    "Colleague's rewards statement refused",
                    "employee",
                    "GET",
                    "/rewards/statement/{id}",
                    {"id": ceo.id},
                    params={"financial_year": FY},
                ),
                Check(
                    "Redeeming a colleague's points refused",
                    "employee",
                    "POST",
                    "/rewards/redemptions",
                    body={"employee_id": str(ceo.id)},
                ),
                Check(
                    "Employee cannot award points",
                    "employee",
                    "POST",
                    "/rewards/awards",
                    body={"employee_id": str(ceo.id)},
                ),
                Check(
                    "Manager cannot award points to themselves",
                    "manager",
                    "POST",
                    "/rewards/awards",
                    body={"employee_id": str(vikram.id)},
                ),
                # Tax
                Check(
                    "Submitting a colleague's tax declaration refused",
                    "employee",
                    "POST",
                    "/tax/declarations/{id}/submit",
                    {"id": ceo_declaration.id},
                ),
                Check(
                    "Attaching proof to a colleague's declaration refused",
                    "employee",
                    "POST",
                    "/tax/proofs",
                    body={"declaration_id": str(ceo_declaration.id)},
                ),
                Check(
                    "Colleague's tax declarations still refused",
                    "employee",
                    "GET",
                    "/tax/declarations",
                    params={"financial_year": FY, "employee_id": str(ceo.id)},
                ),
                # Expenses and GST
                Check(
                    "Filing a claim for a colleague refused",
                    "employee",
                    "POST",
                    "/expenses/claims",
                    body={"employee_id": str(ceo.id)},
                ),
                Check(
                    "Travel request for a colleague refused",
                    "employee",
                    "POST",
                    "/expenses/travel-requests",
                    body={"employee_id": str(ceo.id)},
                ),
                Check(
                    "GST invoice without your own claim refused",
                    "employee",
                    "POST",
                    "/gst/credits",
                    body={},
                ),
                # Culture actions
                Check(
                    "Answering a survey as a colleague refused",
                    "employee",
                    "POST",
                    "/culture/surveys/{id}/responses",
                    {"id": random_id},
                    body={"employee_id": str(ceo.id)},
                ),
                Check(
                    "Acknowledging as a colleague refused",
                    "employee",
                    "POST",
                    "/culture/announcements/{id}/acknowledge",
                    {"id": random_id},
                    body={"employee_id": str(ceo.id)},
                ),
                Check(
                    "Changing a colleague's privacy refused",
                    "employee",
                    "PUT",
                    "/culture/event-privacy",
                    body={"employee_id": str(ceo.id)},
                ),
                Check(
                    "Nominating in a colleague's name refused",
                    "employee",
                    "POST",
                    "/culture/nominations",
                    body={"nominator_employee_id": str(ceo.id)},
                ),
                Check(
                    "Employee cannot create action plans",
                    "employee",
                    "POST",
                    "/culture/action-plans",
                    body={},
                ),
                # Performance
                Check(
                    "Employee cannot recommend increments",
                    "employee",
                    "POST",
                    "/performance/recommendations",
                    body={"employee_id": str(ceo.id)},
                ),
                Check(
                    "Manager refused for somebody outside their line",
                    "manager",
                    "POST",
                    "/performance/recommendations",
                    body={"employee_id": str(ceo.id)},
                ),
                Check(
                    "Manager refused for their own increment",
                    "manager",
                    "POST",
                    "/performance/recommendations",
                    body={"employee_id": str(vikram.id)},
                ),
                Check(
                    "HR refused for their own increment",
                    "hr_admin",
                    "POST",
                    "/performance/recommendations",
                    body={"employee_id": str(priya.id)},
                ),
                Check(
                    "Manager allowed for their own report",
                    "manager",
                    "POST",
                    "/performance/recommendations",
                    body={"employee_id": str(meera.id), "cycle_id": random_id},
                    expect="allow",
                ),
                Check(
                    "Employee cannot record a check-in on their own PIP",
                    "employee",
                    "POST",
                    "/performance/pips/{id}/check-ins",
                    {"id": pip.id},
                    body={},
                ),
                Check(
                    "Manager can record a check-in for their report",
                    "manager",
                    "POST",
                    "/performance/pips/{id}/check-ins",
                    {"id": pip.id},
                    body={},
                    expect="allow",
                ),
                Check(
                    "Employee cannot see increment budgets",
                    "employee",
                    "GET",
                    "/performance/cycles/{id}/budget",
                    {"id": random_id},
                ),
                # Recruiting
                Check(
                    "Referral in a colleague's name refused",
                    "employee",
                    "POST",
                    "/recruiting/referrals",
                    body={"referrer_employee_id": str(ceo.id)},
                ),
                # Autonomy and retention
                Check(
                    "AI proposal queue hidden from employees",
                    "employee",
                    "GET",
                    "/autonomy/transparency",
                    expect="ok",
                    predicate=lambda b: b["pending_queue"] == [] and b["decision_stats"] is None,
                ),
                Check(
                    "Legal hold reasons hidden from employees",
                    "employee",
                    "GET",
                    "/retention/trust-centre",
                    expect="ok",
                    predicate=lambda b: b["open_legal_holds"] == []
                    and "open_legal_holds_count" in b,
                ),
            ]

            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(transport=transport, base_url="http://verify") as client:
                for check in checks:
                    path = check.template.format(
                        **{k: str(v) for k, v in (check.path_values or {}).items()}
                    )
                    json_body = None
                    model = (
                        body_model(check.method, check.template) if check.method != "GET" else None
                    )
                    if model is not None:
                        json_body = build_body(model, check.body or {})
                    response = await client.request(
                        check.method,
                        "/api/v1" + path,
                        params=check.params,
                        json=json_body,
                        headers={"Authorization": f"Bearer {tokens[check.role]}"},
                    )
                    code = response.status_code
                    if code >= 500:
                        await session.rollback()
                    try:
                        payload = response.json()
                    except ValueError:
                        payload = None

                    if code == 422:
                        verdict = "INCONCLUSIVE"
                    elif check.expect == "deny":
                        verdict = "PASS" if code == 403 else "FAIL"
                    elif check.expect == "allow":
                        verdict = "PASS" if code != 403 and code < 500 else "FAIL"
                    else:
                        try:
                            verdict = "PASS" if code == 200 and check.predicate(payload) else "FAIL"
                        except Exception:
                            verdict = "FAIL"
                    detail = ""
                    if verdict != "PASS" and isinstance(payload, dict):
                        detail = str(payload.get("detail", ""))[:90]
                    results.append((verdict, check.role, check.label, f"{code} {detail}".strip()))
        finally:
            app.dependency_overrides.clear()
            await session.close()
            await outer.rollback()
    await engine.dispose()

    width = max(len(r[2]) for r in results)
    for verdict, role, label, outcome in results:
        print(f"  {verdict:<12} {role:<14} {label:<{width}}  {outcome}")
    tally = {v: sum(1 for r in results if r[0] == v) for v in ("PASS", "FAIL", "INCONCLUSIVE")}
    print(
        f"\n{tally['PASS']} passed, {tally['FAIL']} failed, {tally['INCONCLUSIVE']} inconclusive."
    )
    print("Rolled back: nothing was written to the database.")
    return 1 if tally["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
