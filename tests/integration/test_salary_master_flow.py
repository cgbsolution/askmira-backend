"""Salary master: typed amounts, correcting and deleting revisions, the payroll lock.

The gates:
  * A revision payroll has approved or paid on is locked: no edit, no delete.
  * An unpaid revision can be corrected or deleted, audited in the same
    transaction, and the effective-dated chain stays whole.
  * "Update later revisions" carries the typed changes forward, each later
    revision re-resolved on its own CTC, or refuses before writing anything.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import date
from decimal import Decimal

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import create_access_token
from app.db.session import get_session
from app.main import app
from app.models.audit import AuditLog
from app.models.employee import Employee
from app.models.org import LegalEntity
from app.models.payroll import (
    CalculationType,
    ComponentType,
    EmployeeSalary,
    PayrollRun,
    PayrollRunStatus,
    Payslip,
    SalaryComponent,
    SalaryStructure,
    SalaryStructureComponent,
)
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.schemas.payroll import (
    EmployeeSalaryCreate,
    EmployeeSalaryUpdate,
    SalaryComponentAmountInput,
)
from app.services.payroll import salary_master
from app.services.payroll import service as salary_service
from tests.integration.conftest import requires_db

pytestmark = requires_db

PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"


@pytest.fixture(autouse=True)
def _storage_root(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "FILE_STORAGE_ROOT", str(tmp_path / "store"))
    return tmp_path / "store"


async def _structure(session: AsyncSession, tenant: Tenant) -> SalaryStructure:
    """BASIC 40% of CTC, HRA 50% of BASIC, SPECIAL the balance; TELEPHONE outside it."""

    def make(code, name, calc, *, base=None, value=None, order=0):
        return SalaryComponent(
            tenant_id=tenant.id,
            code=code,
            name=name,
            component_type=ComponentType.EARNING,
            calculation_type=calc,
            base_component_code=base,
            value=Decimal(value) if value else None,
            display_order=order,
        )

    basic = make("BASIC", "Basic", CalculationType.PERCENTAGE_OF, base="CTC", value="40", order=1)
    hra = make("HRA", "HRA", CalculationType.PERCENTAGE_OF, base="BASIC", value="50", order=2)
    special = make("SPECIAL", "Special allowance", CalculationType.BALANCE, order=9)
    telephone = make("TELEPHONE", "Telephone allowance", CalculationType.FIXED, value="0", order=5)
    session.add_all([basic, hra, special, telephone])
    await session.flush()

    structure = SalaryStructure(
        tenant_id=tenant.id,
        name="Standard 2026",
        effective_from=date(2025, 4, 1),
        applicable_grades=[],
        applicable_locations=[],
    )
    for index, component in enumerate((basic, hra, special)):
        structure.components.append(
            SalaryStructureComponent(
                tenant_id=tenant.id,
                component_id=component.id,
                component=component,
                display_order=index,
            )
        )
    session.add(structure)
    await session.commit()
    await session.refresh(structure)
    return structure


async def _assign(session, user, employee, structure, ctc, on, components=()) -> EmployeeSalary:
    return await salary_service.assign_salary(
        session,
        user,
        employee.id,
        EmployeeSalaryCreate(
            structure_id=structure.id,
            ctc_annual=Decimal(ctc),
            effective_from=on,
            components=list(components),
        ),
    )


def _update(structure, ctc, on, components=(), *, forward=False) -> EmployeeSalaryUpdate:
    return EmployeeSalaryUpdate(
        structure_id=structure.id,
        ctc_annual=Decimal(ctc),
        effective_from=on,
        components=list(components),
        apply_to_later_revisions=forward,
    )


def _amounts(salary: EmployeeSalary) -> dict[str, Decimal]:
    return {c.component_code: Decimal(str(c.annual_amount)) for c in salary.components}


async def _run_using(
    session: AsyncSession,
    tenant: Tenant,
    legal_entity: LegalEntity,
    employee: Employee,
    salary: EmployeeSalary,
    *,
    month: int,
    run_status: PayrollRunStatus,
) -> PayrollRun:
    run = PayrollRun(
        tenant_id=tenant.id,
        legal_entity_id=legal_entity.id,
        period_year=2026,
        period_month=month,
        effective_date=date(2026, month, 28),
        status=run_status,
    )
    session.add(run)
    await session.flush()
    session.add(
        Payslip(
            tenant_id=tenant.id,
            run_id=run.id,
            employee_id=employee.id,
            employee_salary_id=salary.id,
            working_days=Decimal(30),
            paid_days=Decimal(30),
            gross_earnings=Decimal("100000.00"),
            net_pay=Decimal("100000.00"),
        )
    )
    await session.commit()
    return run


async def _audit(session: AsyncSession, entity_id) -> list[AuditLog]:
    return list(
        (
            await session.scalars(
                select(AuditLog)
                .where(AuditLog.entity_type == "employee_salary", AuditLog.entity_id == entity_id)
                .order_by(AuditLog.created_at)
            )
        ).all()
    )


# ═══════════════════════════════════════════════════════════════════════════
# Typed amounts
# ═══════════════════════════════════════════════════════════════════════════


async def test_typed_amounts_and_remarks_are_stored_and_audited(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    structure = await _structure(session, tenant)
    salary = await _assign(
        session,
        hr_user,
        employee,
        structure,
        "1200000",
        date(2026, 4, 1),
        [
            SalaryComponentAmountInput(component_code="BASIC", amount=Decimal("30000")),
            SalaryComponentAmountInput(
                component_code="TELEPHONE", amount=Decimal("1000"), remarks="Field role"
            ),
            SalaryComponentAmountInput(component_code="HRA", remarks="Metro"),
        ],
    )
    by_code = {c.component_code: c for c in salary.components}
    assert Decimal(str(by_code["BASIC"].annual_amount)) == Decimal("360000")
    assert Decimal(str(by_code["BASIC"].monthly_amount)) == Decimal("30000")
    assert by_code["BASIC"].is_manual
    assert Decimal(str(by_code["HRA"].annual_amount)) == Decimal("180000")
    assert not by_code["HRA"].is_manual
    assert by_code["HRA"].remarks == "Metro"
    assert by_code["TELEPHONE"].is_manual and by_code["TELEPHONE"].remarks == "Field role"
    assert sum(_amounts(salary).values()) == Decimal("1200000")

    [entry] = await _audit(session, salary.id)
    assert entry.changes["typed_amounts"] == {"BASIC": "360000.00", "TELEPHONE": "12000.00"}


async def test_a_monthly_ctc_is_multiplied_on_the_server(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    structure = await _structure(session, tenant)
    salary = await salary_service.assign_salary(
        session,
        hr_user,
        employee.id,
        EmployeeSalaryCreate(
            structure_id=structure.id,
            ctc_monthly=Decimal("51666.67"),
            effective_from=date(2026, 4, 1),
        ),
    )
    # 51,666.67 x 12 = 6,20,000.04, rounded to the structure's whole rupees.
    assert Decimal(str(salary.ctc_annual)) == Decimal("620000")


async def test_a_yearly_typed_amount_is_shown_back_as_typed(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    """A yearly figure that does not divide by twelve keeps its basis, so re-opening
    and saving it again cannot shift it by the monthly rounding."""
    structure = await _structure(session, tenant)
    salary = await _assign(
        session,
        hr_user,
        employee,
        structure,
        "1200000",
        date(2026, 4, 1),
        [
            SalaryComponentAmountInput(
                component_code="TELEPHONE", amount=Decimal("10001"), basis="annual"
            )
        ],
    )
    [card] = await salary_master.list_revisions(session, hr_user, employee.id)
    telephone = next(c for c in card.components if c.component_code == "TELEPHONE")
    assert telephone.pay_basis == "annual"
    assert telephone.annual_amount == Decimal("10001")
    basic = next(c for c in card.components if c.component_code == "BASIC")
    assert basic.pay_basis == "monthly"
    assert salary.id == card.id


# ═══════════════════════════════════════════════════════════════════════════
# Correcting an unpaid revision
# ═══════════════════════════════════════════════════════════════════════════


async def test_an_unpaid_revision_is_corrected_in_place_with_one_audit_entry(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    structure = await _structure(session, tenant)
    first = await _assign(session, hr_user, employee, structure, "600000", date(2025, 6, 1))
    second = await _assign(session, hr_user, employee, structure, "900000", date(2026, 4, 1))

    change = await salary_master.update_salary(
        session,
        hr_user,
        employee.id,
        second.id,
        _update(structure, "960000", date(2026, 3, 1)),
    )
    await session.refresh(first)
    assert Decimal(str(change.salary.ctc_annual)) == Decimal("960000")
    assert change.salary.effective_from == date(2026, 3, 1)
    assert sum(_amounts(change.salary).values()) == Decimal("960000")
    # The revision before it now ends the day before the corrected date.
    assert first.effective_to == date(2026, 2, 28)

    entries = await _audit(session, second.id)
    assert [e.action.value for e in entries] == ["update", "update"]  # assign-as-revision, edit
    edit = entries[-1]
    assert edit.changes["before"]["ctc_annual"] == "900000.00"
    assert edit.changes["after"]["ctc_annual"] == "960000.00"


async def test_a_correction_cannot_jump_over_its_neighbours(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    structure = await _structure(session, tenant)
    first = await _assign(session, hr_user, employee, structure, "600000", date(2025, 6, 1))
    await _assign(session, hr_user, employee, structure, "900000", date(2026, 4, 1))

    with pytest.raises(HTTPException) as caught:
        await salary_master.update_salary(
            session, hr_user, employee.id, first.id, _update(structure, "600000", date(2026, 5, 1))
        )
    assert caught.value.status_code == 422
    assert "before the next one" in caught.value.detail


async def test_a_draft_run_on_the_old_amounts_is_named_for_recompute(
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    employee: Employee,
    legal_entity: LegalEntity,
) -> None:
    structure = await _structure(session, tenant)
    salary = await _assign(session, hr_user, employee, structure, "900000", date(2026, 4, 1))
    await _run_using(
        session, tenant, legal_entity, employee, salary, month=4, run_status=PayrollRunStatus.DRAFT
    )

    change = await salary_master.update_salary(
        session, hr_user, employee.id, salary.id, _update(structure, "960000", date(2026, 4, 1))
    )
    assert change.runs_to_recompute == ["2026-04"]


# ═══════════════════════════════════════════════════════════════════════════
# The payroll lock
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("run_status", [PayrollRunStatus.APPROVED, PayrollRunStatus.PAID])
async def test_a_revision_payroll_has_closed_on_cannot_be_edited_or_deleted(
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    employee: Employee,
    legal_entity: LegalEntity,
    run_status: PayrollRunStatus,
) -> None:
    structure = await _structure(session, tenant)
    salary = await _assign(session, hr_user, employee, structure, "900000", date(2026, 4, 1))
    await _run_using(
        session, tenant, legal_entity, employee, salary, month=4, run_status=run_status
    )

    with pytest.raises(HTTPException) as edit:
        await salary_master.update_salary(
            session, hr_user, employee.id, salary.id, _update(structure, "1", date(2026, 4, 1))
        )
    assert edit.value.status_code == 409
    assert "Apr 2026" in edit.value.detail

    with pytest.raises(HTTPException) as delete:
        await salary_master.delete_salary(session, hr_user, employee.id, salary.id)
    assert delete.value.status_code == 409

    [card] = await salary_master.list_revisions(session, hr_user, employee.id)
    assert card.locked
    assert "Apr 2026" in (card.locked_reason or "")
    # Nothing changed underneath.
    await session.refresh(salary)
    assert Decimal(str(salary.ctc_annual)) == Decimal("900000")


async def test_a_cancelled_run_does_not_lock_the_revision(
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    employee: Employee,
    legal_entity: LegalEntity,
) -> None:
    structure = await _structure(session, tenant)
    salary = await _assign(session, hr_user, employee, structure, "900000", date(2026, 4, 1))
    await _run_using(
        session,
        tenant,
        legal_entity,
        employee,
        salary,
        month=4,
        run_status=PayrollRunStatus.CANCELLED,
    )
    [card] = await salary_master.list_revisions(session, hr_user, employee.id)
    assert not card.locked


# ═══════════════════════════════════════════════════════════════════════════
# Deleting
# ═══════════════════════════════════════════════════════════════════════════


async def test_deleting_the_latest_revision_reopens_the_one_before(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    structure = await _structure(session, tenant)
    first = await _assign(session, hr_user, employee, structure, "600000", date(2025, 6, 1))
    second = await _assign(session, hr_user, employee, structure, "900000", date(2026, 4, 1))

    await salary_master.delete_salary(session, hr_user, employee.id, second.id)

    await session.refresh(first)
    assert first.effective_to is None
    assert await session.get(EmployeeSalary, second.id) is None
    [entry] = [e for e in await _audit(session, second.id) if e.action.value == "delete"]
    assert entry.changes["deleted"]["ctc_annual"] == "900000.00"


async def test_deleting_a_middle_revision_keeps_the_chain_whole(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    structure = await _structure(session, tenant)
    first = await _assign(session, hr_user, employee, structure, "600000", date(2025, 6, 1))
    middle = await _assign(session, hr_user, employee, structure, "700000", date(2025, 12, 1))
    await _assign(session, hr_user, employee, structure, "900000", date(2026, 4, 1))

    await salary_master.delete_salary(session, hr_user, employee.id, middle.id)

    await session.refresh(first)
    assert first.effective_to == date(2026, 3, 31)
    on_jan = await salary_service.active_salary_on(session, hr_user, employee.id, date(2026, 1, 15))
    assert on_jan is not None and on_jan.id == first.id


# ═══════════════════════════════════════════════════════════════════════════
# Update later revisions
# ═══════════════════════════════════════════════════════════════════════════


async def test_typed_changes_carry_into_later_revisions_on_their_own_ctc(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    structure = await _structure(session, tenant)
    first = await _assign(session, hr_user, employee, structure, "600000", date(2025, 6, 1))
    later = await _assign(session, hr_user, employee, structure, "900000", date(2026, 4, 1))

    change = await salary_master.update_salary(
        session,
        hr_user,
        employee.id,
        first.id,
        _update(
            structure,
            "600000",
            date(2025, 6, 1),
            [
                SalaryComponentAmountInput(
                    component_code="TELEPHONE", amount=Decimal("1000"), remarks="Field role"
                )
            ],
            forward=True,
        ),
    )
    assert change.carried_forward_to == [date(2026, 4, 1)]

    await session.refresh(later)
    amounts = _amounts(later)
    assert Decimal(str(later.ctc_annual)) == Decimal("900000")  # its own CTC kept
    assert amounts["TELEPHONE"] == Decimal("12000")
    # 9,00,000 - 3,60,000 basic - 1,80,000 HRA - 12,000 telephone
    assert amounts["SPECIAL"] == Decimal("348000")
    telephone = next(c for c in later.components if c.component_code == "TELEPHONE")
    assert telephone.is_manual and telephone.remarks == "Field role"
    carried = [e for e in await _audit(session, later.id) if "carried_from" in (e.changes or {})]
    assert len(carried) == 1


async def test_without_carry_forward_later_revisions_are_untouched(
    session: AsyncSession, tenant: Tenant, hr_user: User, employee: Employee
) -> None:
    structure = await _structure(session, tenant)
    first = await _assign(session, hr_user, employee, structure, "600000", date(2025, 6, 1))
    later = await _assign(session, hr_user, employee, structure, "900000", date(2026, 4, 1))
    before = _amounts(later)

    await salary_master.update_salary(
        session,
        hr_user,
        employee.id,
        first.id,
        _update(
            structure,
            "600000",
            date(2025, 6, 1),
            [SalaryComponentAmountInput(component_code="TELEPHONE", amount=Decimal("1000"))],
        ),
    )
    await session.refresh(later)
    assert _amounts(later) == before


async def test_carry_forward_refuses_when_a_later_revision_is_paid(
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    employee: Employee,
    legal_entity: LegalEntity,
) -> None:
    structure = await _structure(session, tenant)
    first = await _assign(session, hr_user, employee, structure, "600000", date(2025, 6, 1))
    later = await _assign(session, hr_user, employee, structure, "900000", date(2026, 4, 1))
    await _run_using(
        session, tenant, legal_entity, employee, later, month=4, run_status=PayrollRunStatus.PAID
    )

    with pytest.raises(HTTPException) as caught:
        await salary_master.update_salary(
            session,
            hr_user,
            employee.id,
            first.id,
            _update(
                structure,
                "600000",
                date(2025, 6, 1),
                [SalaryComponentAmountInput(component_code="TELEPHONE", amount=Decimal("1000"))],
                forward=True,
            ),
        )
    assert caught.value.status_code == 409
    await session.refresh(first)
    assert "TELEPHONE" not in _amounts(first)  # nothing written


# ═══════════════════════════════════════════════════════════════════════════
# Through HTTP: preview, attachment, roles and tenants
# ═══════════════════════════════════════════════════════════════════════════


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


async def test_the_preview_resolves_typed_monthly_amounts_and_totals(
    client: httpx.AsyncClient, session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    structure = await _structure(session, tenant)
    response = await client.post(
        f"/api/v1/salary-structures/{structure.id}/preview",
        headers=_auth(hr_user),
        json={
            "ctc_monthly": "100000",
            "components": [
                {"component_code": "BASIC", "amount": "45000", "basis": "monthly"},
                {"component_code": "TELEPHONE", "amount": "24000", "basis": "annual"},
            ],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    by_code = {c["code"]: c for c in body["components"]}
    assert by_code["BASIC"]["annual_amount"] == "540000"
    assert by_code["BASIC"]["is_manual"] is True
    assert by_code["HRA"]["monthly_amount"] == "22500"
    assert by_code["TELEPHONE"]["monthly_amount"] == "2000"
    [earnings] = body["totals"]
    assert earnings["component_type"] == "earning"
    assert earnings["monthly"] == "100000.00"


async def test_the_preview_explains_a_typed_balance(
    client: httpx.AsyncClient, session: AsyncSession, tenant: Tenant, hr_user: User
) -> None:
    structure = await _structure(session, tenant)
    response = await client.post(
        f"/api/v1/salary-structures/{structure.id}/preview",
        headers=_auth(hr_user),
        json={
            "ctc_annual": "1200000",
            "components": [{"component_code": "SPECIAL", "amount": "1"}],
        },
    )
    assert response.status_code == 422
    assert "balance of the CTC" in response.json()["detail"]


async def test_an_attachment_round_trips_and_is_audited(
    client: httpx.AsyncClient,
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    employee: Employee,
) -> None:
    structure = await _structure(session, tenant)
    salary = await _assign(session, hr_user, employee, structure, "900000", date(2026, 4, 1))
    base = f"/api/v1/employees/{employee.id}/salary/{salary.id}/attachment"

    upload = await client.post(
        base,
        headers=_auth(hr_user),
        files={"file": ("Increment letter.pdf", PDF, "application/pdf")},
    )
    assert upload.status_code == 200, upload.text
    assert upload.json()["attachment_name"] == "Increment letter.pdf"

    download = await client.get(base, headers=_auth(hr_user))
    assert download.status_code == 200
    assert download.content == PDF

    removed = await client.delete(base, headers=_auth(hr_user))
    assert removed.status_code == 204
    revisions = await client.get(
        f"/api/v1/employees/{employee.id}/salary/revisions", headers=_auth(hr_user)
    )
    assert revisions.json()[0]["attachment_name"] is None
    actions = [e.action.value for e in await _audit(session, salary.id)]
    assert actions.count("export") == 1


async def test_an_employee_cannot_reach_the_salary_master(
    client: httpx.AsyncClient,
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    employee: Employee,
) -> None:
    structure = await _structure(session, tenant)
    salary = await _assign(session, hr_user, employee, structure, "900000", date(2026, 4, 1))
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

    listing = await client.get(
        f"/api/v1/employees/{employee.id}/salary/revisions", headers=_auth(staff)
    )
    assert listing.status_code == 403
    delete = await client.delete(
        f"/api/v1/employees/{employee.id}/salary/{salary.id}", headers=_auth(staff)
    )
    assert delete.status_code == 403


async def test_another_tenant_cannot_see_or_change_a_revision(
    client: httpx.AsyncClient,
    session: AsyncSession,
    tenant: Tenant,
    hr_user: User,
    other_hr_user: User,
    employee: Employee,
) -> None:
    structure = await _structure(session, tenant)
    salary = await _assign(session, hr_user, employee, structure, "900000", date(2026, 4, 1))

    listing = await client.get(
        f"/api/v1/employees/{employee.id}/salary/revisions", headers=_auth(other_hr_user)
    )
    assert listing.status_code == 404
    delete = await client.delete(
        f"/api/v1/employees/{employee.id}/salary/{salary.id}", headers=_auth(other_hr_user)
    )
    assert delete.status_code == 404
    assert await session.get(EmployeeSalary, salary.id) is not None
