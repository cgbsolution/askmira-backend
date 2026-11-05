"""Payroll → Salary structures: templates HR builds, and the Salary master's Calculate.

The gates:
  * A template is saved only when it can split a CTC (one balance, rules that
    read, wage types that exist and are on), audited in the same transaction.
  * Calculate writes nothing; its figures saved through the Salary master are
    stored exactly as shown.
  * A salary on a template never stores the employer contributions kept inside
    the CTC: payroll works those out every month.
  * A structure used by a salary or an offer is never deleted; the only
    structure switched on cannot be switched off.
  * Another tenant cannot see or use a workspace's templates; an employee
    cannot reach them at all.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import date
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token
from app.db.session import get_session
from app.main import app
from app.models.audit import AuditLog
from app.models.employee import Employee
from app.models.payroll import EmployeeSalary, SalaryComponent, SalaryStructure
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.schemas.payroll import EmployeeSalaryCreate, SalaryComponentAmountInput
from app.services.payroll import service as salary_service
from app.services.payroll import standard_setup
from app.services.payroll.salary_structure import SalaryStructureError, resolve_ctc
from tests.integration.conftest import requires_db

pytestmark = requires_db


@pytest.fixture
async def client(session: AsyncSession) -> AsyncGenerator[httpx.AsyncClient, None]:
    async def override() -> AsyncGenerator[AsyncSession, None]:
        yield session

    app.dependency_overrides[get_session] = override
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as c:
            yield c
    finally:
        app.dependency_overrides.clear()


def _auth(user: User) -> dict[str, str]:
    token = create_access_token(
        user_id=str(user.id), tenant_id=str(user.tenant_id), role=user.role.value
    )
    return {"Authorization": f"Bearer {token}"}


PF = {"method": "percent", "value": "13", "of": "PF_WAGES", "ceiling": "15000"}
ESI = {
    "method": "percent",
    "value": "3.25",
    "of": "ESI_WAGES",
    "when": {"on": "ESI_WAGES", "op": "up_to", "amount": "21000"},
    "rounding": "up",
}
PT_SLABS = {
    "method": "slab",
    "of": "GROSS",
    "slabs": [{"up_to": "15000", "amount": "0"}, {"up_to": None, "amount": "200"}],
}


def _template(name: str = "Staff CTC", **extra) -> dict:
    return {
        "name": name,
        "rounding_precision": 0,
        "lines": [
            {
                "component_code": "GRATUITY",
                "rule": {"method": "percent", "value": "4.81", "of": "BASIC"},
            },
            {"component_code": "BASIC", "rule": {"method": "percent", "value": "50", "of": "CTC"}},
            {"component_code": "HRA", "rule": {"method": "percent", "value": "40", "of": "BASIC"}},
            {"component_code": "SPECIAL", "rule": {"method": "balance"}},
            {"component_code": "PT_MANUAL", "rule": PT_SLABS},
        ],
        "employer_pf": PF,
        "employer_esi": ESI,
        **extra,
    }


async def _setup(session: AsyncSession, user: User) -> None:
    """The workspace's wage types (and its Standard CTC), as the one-click setup makes them."""
    await standard_setup.install_standard(session, user)


async def _create(client: httpx.AsyncClient, user: User, body: dict | None = None) -> dict:
    response = await client.post(
        "/api/v1/salary-templates", headers=_auth(user), json=body or _template()
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _audit(session: AsyncSession, entity_id) -> list[AuditLog]:
    return list(
        (
            await session.scalars(
                select(AuditLog)
                .where(AuditLog.entity_type == "salary_structure", AuditLog.entity_id == entity_id)
                .order_by(AuditLog.created_at)
            )
        ).all()
    )


# ═══════════════════════════════════════════════════════════════════════════
# Building a template
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_template_is_saved_read_back_and_audited(
    client: httpx.AsyncClient, session: AsyncSession, hr_user: User
) -> None:
    await _setup(session, hr_user)
    created = await _create(client, hr_user)
    assert created["is_template"] is True
    by_code = {line["component_code"]: line for line in created["lines"]}
    assert by_code["BASIC"]["rule"] == {"method": "percent", "value": "50", "of": "CTC"}
    assert by_code["BASIC"]["rule_text"] == "50% of CTC"
    assert by_code["HRA"]["rule_text"] == "40% of BASIC SALARY"
    assert by_code["PT_MANUAL"]["rule_text"].startswith("By gross a month")
    assert created["employer_pf_text"] == "13% of PF wages up to ₹15,000"

    again = await client.get(f"/api/v1/salary-templates/{created['id']}", headers=_auth(hr_user))
    assert again.status_code == 200
    assert again.json()["lines"] == created["lines"]

    listing = await client.get("/api/v1/salary-templates", headers=_auth(hr_user))
    mine = next(row for row in listing.json() if row["id"] == created["id"])
    assert mine["is_template"] and mine["line_count"] == 5
    assert "BASIC SALARY 50% of CTC" in mine["summary"]
    assert mine["employer_in_ctc"] == ["EMPLOYER PF CONTRIBUTION", "EMPLOYER ESIC CONTRIBUTION"]

    [entry] = await _audit(session, created["id"])
    assert entry.action.value == "create"
    assert entry.changes["rules"]["BASIC"] == "50% of CTC"


async def test_a_template_edit_is_audited_with_what_changed(
    client: httpx.AsyncClient, session: AsyncSession, hr_user: User
) -> None:
    await _setup(session, hr_user)
    created = await _create(client, hr_user)
    body = _template(name="Staff CTC 2027")
    body["lines"][1]["rule"]["value"] = "45"
    body["employer_esi"] = None
    response = await client.put(
        f"/api/v1/salary-templates/{created['id']}", headers=_auth(hr_user), json=body
    )
    assert response.status_code == 200, response.text
    assert response.json()["employer_esi"] is None
    entries = await _audit(session, created["id"])
    assert [e.action.value for e in entries] == ["create", "update"]
    changes = entries[-1].changes
    assert changes["name"] == ["Staff CTC", "Staff CTC 2027"]
    assert changes["rules"]["BASIC"] == ["50% of CTC", "45% of CTC"]
    assert changes["rules"]["EMPLOYER_ESI"][1] is None


@pytest.mark.parametrize(
    ("change", "status", "message"),
    [
        (
            lambda b: b["lines"].append(
                {"component_code": "NOPE", "rule": {"method": "fixed", "value": "1"}}
            ),
            422,
            "Unknown wage type",
        ),
        (lambda b: b["lines"][1].update(rule={"method": "balance"}), 422, "Only one wage type"),
        (lambda b: b["lines"].pop(3), 422, "balance of the CTC"),
        (lambda b: b["lines"][2]["rule"].update(of="BONUS"), 422, "BONUS, which is not in"),
        (lambda b: b["lines"][1]["rule"].update(value="150"), 422, "100"),
        (lambda b: b.update(name="   "), 422, "name"),
    ],
)
async def test_a_template_that_cannot_split_a_ctc_is_not_saved(
    client: httpx.AsyncClient, session: AsyncSession, hr_user: User, change, status, message
) -> None:
    await _setup(session, hr_user)
    body = _template()
    change(body)
    response = await client.post("/api/v1/salary-templates", headers=_auth(hr_user), json=body)
    assert response.status_code == status, response.text
    assert message.lower() in response.text.lower()
    names = (await session.scalars(select(SalaryStructure.name))).all()
    assert "Staff CTC" not in names


async def test_a_switched_off_wage_type_or_a_taken_name_is_refused(
    client: httpx.AsyncClient, session: AsyncSession, hr_user: User
) -> None:
    await _setup(session, hr_user)
    await _create(client, hr_user)
    clash = await client.post(
        "/api/v1/salary-templates", headers=_auth(hr_user), json=_template(name="staff ctc")
    )
    assert clash.status_code == 409

    hra = await session.scalar(select(SalaryComponent).where(SalaryComponent.code == "HRA"))
    hra.active = False
    await session.commit()
    off = await client.post(
        "/api/v1/salary-templates", headers=_auth(hr_user), json=_template(name="Other")
    )
    assert off.status_code == 422
    assert "switched off" in off.json()["detail"]


async def test_a_new_structure_starts_from_the_usual_split(
    client: httpx.AsyncClient, session: AsyncSession, hr_user: User
) -> None:
    await _setup(session, hr_user)
    response = await client.get("/api/v1/salary-templates/new", headers=_auth(hr_user))
    assert response.status_code == 200
    draft = response.json()
    rules = {line["component_code"]: line["rule"] for line in draft["lines"]}
    assert rules["BASIC"]["value"] == "50" and rules["BASIC"]["of"] == "CTC"
    assert rules["SPECIAL"]["method"] == "balance"
    assert draft["employer_pf"]["ceiling"] == "15000"
    # Nothing is saved by opening the editor.
    assert len((await session.scalars(select(SalaryStructure))).all()) == 1


# ═══════════════════════════════════════════════════════════════════════════
# Calculate, and saving what it filled in
# ═══════════════════════════════════════════════════════════════════════════


async def test_calculate_splits_a_monthly_ctc_and_writes_nothing(
    client: httpx.AsyncClient, session: AsyncSession, hr_user: User
) -> None:
    await _setup(session, hr_user)
    created = await _create(client, hr_user)
    response = await client.post(
        f"/api/v1/salary-templates/{created['id']}/calculate",
        headers=_auth(hr_user),
        json={"ctc_amount": "51666.67", "ctc_basis": "monthly"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    lines = {line["component_code"]: line for line in body["lines"]}
    assert lines["BASIC"]["amount"] == "25833.00" and lines["BASIC"]["basis"] == "monthly"
    assert lines["BASIC"]["annual"] == "309996.00"
    assert lines["HRA"]["amount"] == "10333.00"
    assert lines["PT_MANUAL"]["amount"] == "200.00"
    assert body["gross_monthly"] == "48473.00"
    employer = {row["key"]: row for row in body["employer"]}
    assert employer["EMPLOYER_PF"]["monthly"] == "1950.00"
    assert employer["EMPLOYER_ESI"]["applied"] is False
    assert body["unallocated_monthly"] == "0.67"
    assert any("payroll works out" in note for note in body["notes"])
    assert (await session.scalars(select(EmployeeSalary))).all() == []


async def test_calculate_on_a_yearly_ctc_fills_yearly_amounts(
    client: httpx.AsyncClient, session: AsyncSession, hr_user: User
) -> None:
    await _setup(session, hr_user)
    created = await _create(client, hr_user)
    response = await client.post(
        f"/api/v1/salary-templates/{created['id']}/calculate",
        headers=_auth(hr_user),
        json={"ctc_amount": "1200000", "ctc_basis": "annual"},
    )
    lines = {line["component_code"]: line for line in response.json()["lines"]}
    assert lines["BASIC"]["basis"] == "annual"
    assert lines["BASIC"]["amount"] == "600000.00"
    assert lines["PT_MANUAL"]["amount"] == "2400.00"


async def test_calculate_refuses_a_ctc_the_lines_do_not_fit(
    client: httpx.AsyncClient, session: AsyncSession, hr_user: User
) -> None:
    await _setup(session, hr_user)
    body = _template()
    body["lines"].append(
        {"component_code": "CONVEYANCE", "rule": {"method": "fixed", "value": "20000"}}
    )
    created = await _create(client, hr_user, body)
    response = await client.post(
        f"/api/v1/salary-templates/{created['id']}/calculate",
        headers=_auth(hr_user),
        json={"ctc_amount": "25000", "ctc_basis": "monthly"},
    )
    assert response.status_code == 422
    assert "more than the CTC" in response.json()["detail"]
    bad = await client.post(
        f"/api/v1/salary-templates/{created['id']}/calculate",
        headers=_auth(hr_user),
        json={"ctc_amount": "abc", "ctc_basis": "monthly"},
    )
    assert bad.status_code == 422


async def test_the_editor_preview_splits_an_unsaved_template(
    client: httpx.AsyncClient, session: AsyncSession, hr_user: User
) -> None:
    await _setup(session, hr_user)
    response = await client.post(
        "/api/v1/salary-templates/preview",
        headers=_auth(hr_user),
        json={"template": _template(), "ctc_amount": "20000", "ctc_basis": "monthly"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    employer = {row["key"]: row for row in body["employer"]}
    assert employer["EMPLOYER_ESI"]["applied"] is True
    assert body["allocated_monthly"] == "20000.00"
    assert len((await session.scalars(select(SalaryStructure))).all()) == 1


async def test_the_calculated_figures_save_exactly_through_the_salary_master(
    client: httpx.AsyncClient, session: AsyncSession, hr_user: User, employee: Employee
) -> None:
    await _setup(session, hr_user)
    created = await _create(client, hr_user)
    calc = (
        await client.post(
            f"/api/v1/salary-templates/{created['id']}/calculate",
            headers=_auth(hr_user),
            json={"ctc_amount": "620000", "ctc_basis": "annual"},
        )
    ).json()
    # HR reviews and changes one figure before submitting.
    components = [
        {"component_code": line["component_code"], "amount": line["amount"], "basis": line["basis"]}
        for line in calc["lines"]
        if line["amount"] != "0.00"
    ]
    next(c for c in components if c["component_code"] == "HRA")["remarks"] = "Reviewed"
    saved = await client.post(
        f"/api/v1/employees/{employee.id}/salary",
        headers=_auth(hr_user),
        json={
            "entry": "manual",
            "structure_id": created["id"],
            "effective_from": "2026-04-01",
            "ctc_amount": "620000",
            "ctc_basis": "annual",
            "components": components,
        },
    )
    assert saved.status_code == 201, saved.text
    [revision] = (
        await client.get(
            f"/api/v1/employees/{employee.id}/salary/revisions", headers=_auth(hr_user)
        )
    ).json()
    assert revision["structure_name"] == "Staff CTC"
    stored = {c["component_code"]: c for c in revision["components"]}
    for line in calc["lines"]:
        if line["amount"] == "0.00":
            continue
        assert stored[line["component_code"]]["annual_amount"] == line["annual"]
        assert stored[line["component_code"]]["pay_basis"] == "annual"
    assert stored["HRA"]["remarks"] == "Reviewed"
    # Employer PF / ESI are payroll's to work out, never a stored wage type.
    assert "EMPLOYER_PF" not in stored and "EMPLOYER_ESI" not in stored
    assert revision["ctc_annual"] == "620000.00"


async def test_a_salary_assigned_by_ctc_alone_is_split_by_the_template(
    session: AsyncSession, hr_user: User, employee: Employee
) -> None:
    """Offers, increments and the bulk upload's CTC-only rows give just a CTC."""
    await _setup(session, hr_user)
    from app.schemas.payroll import SalaryTemplateInput
    from app.services.payroll import templates

    read = await templates.create_template(
        session, hr_user, SalaryTemplateInput.model_validate(_template())
    )
    salary = await salary_service.assign_salary(
        session,
        hr_user,
        employee.id,
        EmployeeSalaryCreate(
            structure_id=read.id,
            ctc_monthly=Decimal("50000"),
            effective_from=date(2026, 4, 1),
            components=[SalaryComponentAmountInput(component_code="HRA", amount=Decimal("8000"))],
        ),
    )
    by_code = {c.component_code: c for c in salary.components}
    assert Decimal(str(by_code["BASIC"].monthly_amount)) == Decimal("25000")
    assert Decimal(str(by_code["HRA"].monthly_amount)) == Decimal("8000")
    assert by_code["HRA"].is_manual and not by_code["BASIC"].is_manual
    assert "EMPLOYER_PF" not in by_code
    in_ctc = sum(Decimal(str(c.monthly_amount)) for c in salary.components if c.part_of_ctc)
    # The CTC less the 1,950 employer PF payroll adds each month.
    assert in_ctc == Decimal("50000") - Decimal("1950")
    assert Decimal(str(salary.ctc_annual)) == Decimal("600000")


async def test_an_offer_breakup_lists_the_employer_contributions_inside_the_ctc(
    session: AsyncSession, hr_user: User
) -> None:
    await _setup(session, hr_user)
    from app.schemas.payroll import SalaryTemplateInput
    from app.services.payroll import templates

    read = await templates.create_template(
        session, hr_user, SalaryTemplateInput.model_validate(_template())
    )
    structure = await session.get(SalaryStructure, read.id)
    breakdown = salary_service.structure_breakdown(structure, Decimal("600000"))
    codes = {c.code for c in breakdown.components}
    assert "EMPLOYER_PF" in codes
    assert breakdown.annual_total_in_ctc == Decimal("600000")
    with pytest.raises(SalaryStructureError, match="template"):
        resolve_ctc(structure, Decimal("600000"))


# ═══════════════════════════════════════════════════════════════════════════
# A structure of the earlier kind
# ═══════════════════════════════════════════════════════════════════════════


async def test_the_standard_ctc_is_shown_as_rules_calculates_and_converts_on_save(
    client: httpx.AsyncClient, session: AsyncSession, hr_user: User, employee: Employee
) -> None:
    await _setup(session, hr_user)
    standard = await session.scalar(select(SalaryStructure))
    old = await salary_service.assign_salary(
        session,
        hr_user,
        employee.id,
        EmployeeSalaryCreate(
            structure_id=standard.id, ctc_annual=Decimal("600000"), effective_from=date(2026, 4, 1)
        ),
    )
    before = {c.component_code: Decimal(str(c.annual_amount)) for c in old.components}

    shown = (
        await client.get(f"/api/v1/salary-templates/{standard.id}", headers=_auth(hr_user))
    ).json()
    assert shown["is_template"] is False and shown["used_by"] == 1
    rules = {line["component_code"]: line["rule_text"] for line in shown["lines"]}
    assert rules["GRATUITY"] == "4.81% of BASIC SALARY"
    assert rules["SPECIAL"] == "Balance of CTC"
    assert shown["employer_pf"] is None  # the earlier kind leaves PF to payroll, on top

    calc = await client.post(
        f"/api/v1/salary-templates/{standard.id}/calculate",
        headers=_auth(hr_user),
        json={"ctc_amount": "50000", "ctc_basis": "monthly"},
    )
    assert calc.status_code == 200, calc.text

    body = {
        "name": shown["name"],
        "rounding_precision": 0,
        "lines": [
            {"component_code": x["component_code"], "rule": x["rule"]} for x in shown["lines"]
        ],
        "employer_pf": PF,
        "employer_esi": None,
    }
    saved = await client.put(
        f"/api/v1/salary-templates/{standard.id}", headers=_auth(hr_user), json=body
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["is_template"] is True
    await session.refresh(old)
    assert {c.component_code: Decimal(str(c.annual_amount)) for c in old.components} == before
    entries = await _audit(session, standard.id)
    assert entries[-1].changes.get("made_template") is True


# ═══════════════════════════════════════════════════════════════════════════
# Switching off, deleting, roles and tenants
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_used_structure_is_kept_and_the_last_one_on_stays_on(
    client: httpx.AsyncClient, session: AsyncSession, hr_user: User, employee: Employee
) -> None:
    await _setup(session, hr_user)
    standard = await session.scalar(select(SalaryStructure))
    await salary_service.assign_salary(
        session,
        hr_user,
        employee.id,
        EmployeeSalaryCreate(
            structure_id=standard.id, ctc_annual=Decimal("600000"), effective_from=date(2026, 4, 1)
        ),
    )
    used = await client.delete(f"/api/v1/salary-templates/{standard.id}", headers=_auth(hr_user))
    assert used.status_code == 409
    assert "1 person's salary" in used.json()["detail"]

    last = await client.patch(
        f"/api/v1/salary-templates/{standard.id}", headers=_auth(hr_user), json={"active": False}
    )
    assert last.status_code == 409

    created = await _create(client, hr_user)
    off = await client.patch(
        f"/api/v1/salary-templates/{standard.id}", headers=_auth(hr_user), json={"active": False}
    )
    assert off.status_code == 200 and off.json()["active"] is False

    gone = await client.delete(f"/api/v1/salary-templates/{created['id']}", headers=_auth(hr_user))
    # Unused, but now the only structure switched on: the Salary master needs one.
    assert gone.status_code == 409
    assert "only salary structure switched on" in gone.json()["detail"]


async def test_an_unused_structure_can_be_deleted(
    client: httpx.AsyncClient, session: AsyncSession, hr_user: User
) -> None:
    await _setup(session, hr_user)
    created = await _create(client, hr_user)
    gone = await client.delete(f"/api/v1/salary-templates/{created['id']}", headers=_auth(hr_user))
    assert gone.status_code == 204
    assert await session.get(SalaryStructure, created["id"]) is None
    actions = [e.action.value for e in await _audit(session, created["id"])]
    assert actions == ["create", "delete"]


async def test_another_tenant_and_an_employee_are_kept_out(
    client: httpx.AsyncClient,
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    other_hr_user: User,
    employee: Employee,
) -> None:
    await _setup(session, hr_user)
    created = await _create(client, hr_user)
    for method, url, body in (
        ("GET", f"/api/v1/salary-templates/{created['id']}", None),
        ("PUT", f"/api/v1/salary-templates/{created['id']}", _template()),
        (
            "POST",
            f"/api/v1/salary-templates/{created['id']}/calculate",
            {"ctc_amount": "50000", "ctc_basis": "monthly"},
        ),
        ("DELETE", f"/api/v1/salary-templates/{created['id']}", None),
    ):
        response = await client.request(method, url, headers=_auth(other_hr_user), json=body)
        assert response.status_code == 404, (method, url, response.text)
    listing = await client.get("/api/v1/salary-templates", headers=_auth(other_hr_user))
    assert all(row["id"] != created["id"] for row in listing.json())

    staff = User(
        tenant_id=tenant.id,
        email="staff@acme.test",
        password_hash="x",
        full_name="Staff",
        role=UserRole.EMPLOYEE,
        is_active=True,
        employee_id=employee.id,
    )
    session.add(staff)
    await session.commit()
    await session.refresh(staff)
    denied = await client.get("/api/v1/salary-templates", headers=_auth(staff))
    assert denied.status_code == 403
