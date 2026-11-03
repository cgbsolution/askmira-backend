"""HR downloads or emails the payslips of chosen people for the last 1, 3, 6 or 12 months.

Runs and payslips are inserted directly (approved or paid, published or not),
so these tests depend on nothing but the batch itself. The background worker
is not started inside the test suite; each test calls `process` the way the
worker would.
"""

from __future__ import annotations

import calendar
import io
import uuid
import zipfile
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from pypdf import PdfReader
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.audit import AuditLog
from app.models.employee import Employee, EmploymentStatus
from app.models.notifications import Channel
from app.models.org import LegalEntity, Location
from app.models.payroll import PayrollRun, PayrollRunStatus, Payslip
from app.models.payslip_batch import PayslipBatch
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services.notifications import dispatch
from app.services.notifications.adapters import ConsoleEmailAdapter, OutboundMessage
from app.services.payroll import payslip_batches
from tests.integration.conftest import requires_db
from tests.integration.platform_support import bearer, client  # noqa: F401  (fixture)

pytestmark = requires_db

BASE = "/api/v1/payroll/payslip-batches"


@pytest.fixture(autouse=True)
def _quick(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "FILE_STORAGE_ROOT", str(tmp_path))
    monkeypatch.setattr(payslip_batches, "SEND_INTERVAL", 0)
    monkeypatch.setattr(payslip_batches, "RETRY_WAITS", (0, 0, 0))
    original = dict(dispatch._ADAPTERS)
    yield
    dispatch._ADAPTERS.clear()
    dispatch._ADAPTERS.update(original)


@pytest.fixture
def mail() -> ConsoleEmailAdapter:
    adapter = ConsoleEmailAdapter()
    dispatch.register_adapter(Channel.EMAIL, adapter)
    return adapter


async def _person(
    session: AsyncSession,
    tenant: Tenant,
    entity: LegalEntity,
    location: Location,
    code: str,
    first: str,
    *,
    work_email: str | None = None,
    personal_email: str | None = None,
    status: EmploymentStatus = EmploymentStatus.ACTIVE,
) -> Employee:
    person = Employee(
        tenant_id=tenant.id,
        employee_code=code,
        first_name=first,
        last_name="Test",
        work_email=work_email if work_email is not None else f"{code.lower()}@acme.test",
        personal_email=personal_email,
        legal_entity_id=entity.id,
        location_id=location.id,
        status=status,
        date_of_joining=date(2024, 4, 1),
        pan="ABCDE1234F",
        uan="100200300400",
        designation="Engineer",
    )
    session.add(person)
    await session.commit()
    await session.refresh(person)
    return person


async def _month(
    session: AsyncSession,
    tenant: Tenant,
    entity: LegalEntity,
    people: list[Employee],
    year: int,
    month: int,
    *,
    run_status: PayrollRunStatus = PayrollRunStatus.PAID,
    published: bool = True,
) -> PayrollRun:
    """A month's run whose payslips add up: 30,000 + 12,000 earned, 1,800 PF."""
    run = PayrollRun(
        tenant_id=tenant.id,
        legal_entity_id=entity.id,
        period_year=year,
        period_month=month,
        effective_date=date(year, month, calendar.monthrange(year, month)[1]),
        status=run_status,
    )
    session.add(run)
    await session.flush()
    for person in people:
        session.add(
            Payslip(
                tenant_id=tenant.id,
                run_id=run.id,
                employee_id=person.id,
                working_days=Decimal(30),
                paid_days=Decimal(30),
                gross_earnings=Decimal("42000.00"),
                total_deductions=Decimal("1800.00"),
                net_pay=Decimal("40200.00"),
                employer_cost=Decimal("43800.00"),
                pf_employee=Decimal("1800.00"),
                line_items=[
                    {
                        "code": "BASIC",
                        "name": "BASIC SALARY",
                        "type": "earning",
                        "amount": "30000.00",
                    },
                    {
                        "code": "HRA",
                        "name": "HOUSE RENT ALLOWANCE",
                        "type": "earning",
                        "amount": "12000.00",
                    },
                    {
                        "code": "PF_EMPLOYEE",
                        "name": "Provident fund (employee)",
                        "type": "deduction",
                        "amount": "1800.00",
                    },
                    {
                        "code": "PF_EMPLOYER",
                        "name": "Provident fund (employer)",
                        "type": "employer_contribution",
                        "amount": "1800.00",
                    },
                ],
                input_lines=[],
                is_published=published,
                published_at=datetime.now(UTC) if published else None,
            )
        )
    await session.commit()
    return run


@pytest.fixture
async def org(session, tenant, legal_entity, location, hr_user):
    """Three people. Jun to Sep 2026 paid and released; Oct 2026 approved, not yet
    released; Nov 2026 still a draft. Asha left in August."""
    asha = await _person(
        session,
        tenant,
        legal_entity,
        location,
        "E001",
        "Asha",
        personal_email="asha.home@mail.test",
        status=EmploymentStatus.EXITED,
    )
    bina = await _person(session, tenant, legal_entity, location, "E002", "Bina")
    chet = await _person(session, tenant, legal_entity, location, "E003", "Chet")
    await _month(session, tenant, legal_entity, [asha, bina, chet], 2026, 6)
    await _month(session, tenant, legal_entity, [asha, bina, chet], 2026, 7)
    await _month(session, tenant, legal_entity, [asha, bina, chet], 2026, 8)
    await _month(session, tenant, legal_entity, [bina, chet], 2026, 9)
    await _month(
        session,
        tenant,
        legal_entity,
        [bina, chet],
        2026,
        10,
        run_status=PayrollRunStatus.APPROVED,
        published=False,
    )
    await _month(
        session,
        tenant,
        legal_entity,
        [bina, chet],
        2026,
        11,
        run_status=PayrollRunStatus.DRAFT,
        published=False,
    )
    return {"asha": asha, "bina": bina, "chet": chet, "hr": hr_user}


async def _batch(session: AsyncSession, batch_id: str) -> PayslipBatch:
    return await session.get(PayslipBatch, uuid.UUID(batch_id), populate_existing=True)


# ── Pure ────────────────────────────────────────────────────────────────────


def test_the_window_counts_back_from_the_month_named() -> None:
    assert payslip_batches.window(date(2026, 9, 17), 1) == (date(2026, 9, 1), date(2026, 9, 1))
    assert payslip_batches.window(date(2026, 9, 1), 3) == (date(2026, 7, 1), date(2026, 9, 1))
    assert payslip_batches.window(date(2026, 2, 1), 6) == (date(2025, 9, 1), date(2026, 2, 1))
    assert payslip_batches.window(date(2026, 9, 1), 12) == (date(2025, 10, 1), date(2026, 9, 1))
    assert payslip_batches.window_label(date(2026, 9, 1), date(2026, 9, 1)) == "September 2026"
    assert (
        payslip_batches.window_label(date(2026, 7, 1), date(2026, 9, 1))
        == "July 2026 to September 2026"
    )


# ── Who has payslips ────────────────────────────────────────────────────────


async def test_people_counts_the_payslips_in_the_period(client: httpx.AsyncClient, org) -> None:
    response = await client.get(f"{BASE}/people", params={"months": 3}, headers=bearer(org["hr"]))
    assert response.status_code == 200, response.text
    body = response.json()
    # The latest approved month is October; November is only a draft.
    assert body["latest_month"] == "2026-10-01"
    assert body["period_label"] == "August 2026 to October 2026"
    assert body["email_ready"] is False  # no mail server in the test suite
    rows = {row["employee_code"]: row for row in body["people"]}
    assert set(rows) == {"E001", "E002", "E003"}
    assert (rows["E001"]["payslips"], rows["E001"]["released"]) == (1, 1)
    # October is approved but not released: downloadable, not emailable.
    assert (rows["E002"]["payslips"], rows["E002"]["released"]) == (3, 2)
    assert rows["E001"]["employment_status"] == "exited"

    earlier = await client.get(
        f"{BASE}/people", params={"months": 6, "to": "2026-08-15"}, headers=bearer(org["hr"])
    )
    assert earlier.json()["period_label"] == "March 2026 to August 2026"
    assert {r["employee_code"]: r["payslips"] for r in earlier.json()["people"]} == {
        "E001": 3,
        "E002": 3,
        "E003": 3,
    }

    bad = await client.get(f"{BASE}/people", params={"months": 2}, headers=bearer(org["hr"]))
    assert bad.status_code == 422


# ── Download ────────────────────────────────────────────────────────────────


async def test_a_download_is_one_zip_of_pdfs(
    client: httpx.AsyncClient, session: AsyncSession, org, tenant
) -> None:
    ids = [str(org["bina"].id), str(org["asha"].id)]
    response = await client.post(
        f"{BASE}/export", json={"employee_ids": ids, "months": 3}, headers=bearer(org["hr"])
    )
    assert response.status_code == 202, response.text
    batch = response.json()
    assert batch["status"] == "queued"
    assert batch["period_label"] == "August 2026 to October 2026"
    assert (batch["total_people"], batch["total_payslips"]) == (2, 4)

    not_ready = await client.get(f"{BASE}/{batch['id']}/file", headers=bearer(org["hr"]))
    assert not_ready.status_code == 409

    await payslip_batches.process(session, tenant.id, uuid.UUID(batch["id"]))

    done = (await client.get(f"{BASE}/{batch['id']}", headers=bearer(org["hr"]))).json()
    assert done["status"] == "completed"
    assert (done["succeeded"], done["skipped"], done["failed"]) == (2, 0, 0)
    assert done["file_available"] is True
    assert done["file_name"] == "payslips-Aug-2026-to-Oct-2026.zip"
    results = {r["employee_code"]: r for r in done["results"]}
    assert results["E002"]["payslips"] == 3 and results["E001"]["payslips"] == 1

    file = await client.get(f"{BASE}/{batch['id']}/file", headers=bearer(org["hr"]))
    assert file.status_code == 200
    assert file.headers["content-type"] == "application/zip"
    names = sorted(zipfile.ZipFile(io.BytesIO(file.content)).namelist())
    assert names == [
        "payslip-E001-2026-08.pdf",
        "payslip-E002-2026-08.pdf",
        "payslip-E002-2026-09.pdf",
        "payslip-E002-2026-10.pdf",
    ]
    pdf = zipfile.ZipFile(io.BytesIO(file.content)).read("payslip-E002-2026-09.pdf")
    text = PdfReader(io.BytesIO(pdf)).pages[0].extract_text()
    assert "Payslip for September 2026" in text and "40,200.00" in text

    summaries = (
        await session.scalars(
            select(AuditLog.summary).where(AuditLog.entity_type == "payslip_batch")
        )
    ).all()
    assert any("Asked for the payslips of 2 people" in s for s in summaries)
    assert any("Downloaded the payslips of 2 people" in s for s in summaries)


async def test_one_payslip_comes_as_the_pdf_itself(
    client: httpx.AsyncClient, session: AsyncSession, org, tenant
) -> None:
    response = await client.post(
        f"{BASE}/export",
        json={"employee_ids": [str(org["chet"].id)], "months": 1, "to": "2026-09-01"},
        headers=bearer(org["hr"]),
    )
    assert response.status_code == 202, response.text
    await payslip_batches.process(session, tenant.id, uuid.UUID(response.json()["id"]))
    file = await client.get(f"{BASE}/{response.json()['id']}/file", headers=bearer(org["hr"]))
    assert file.headers["content-type"] == "application/pdf"
    assert 'filename="payslip-E003-2026-09.pdf"' in file.headers["content-disposition"]
    assert file.content.startswith(b"%PDF")


async def test_a_day_old_file_is_gone_and_the_sweep_removes_it(
    client: httpx.AsyncClient, session: AsyncSession, org, tenant
) -> None:
    response = await client.post(
        f"{BASE}/export",
        json={"employee_ids": [str(org["bina"].id)], "months": 3},
        headers=bearer(org["hr"]),
    )
    batch_id = response.json()["id"]
    await payslip_batches.process(session, tenant.id, uuid.UUID(batch_id))
    batch = await _batch(session, batch_id)
    batch.file_expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await session.commit()

    gone = await client.get(f"{BASE}/{batch_id}/file", headers=bearer(org["hr"]))
    assert gone.status_code == 410
    from app.services.scheduler.jobs import system_actor

    outcome = await payslip_batches.sweep(session, system_actor(tenant.id))
    assert outcome["files_removed"] == 1
    assert (await _batch(session, batch_id)).file_key is None


# ── Email ───────────────────────────────────────────────────────────────────


async def test_each_person_is_emailed_their_released_payslips(
    client: httpx.AsyncClient, session: AsyncSession, org, tenant, mail
) -> None:
    ids = [str(org["asha"].id), str(org["bina"].id), str(org["chet"].id)]
    response = await client.post(
        f"{BASE}/email",
        json={"employee_ids": ids, "months": 3, "send_to": "work"},
        headers=bearer(org["hr"]),
    )
    assert response.status_code == 202, response.text
    # Released payslips only: Asha 1 (Aug), Bina and Chet 2 each (Aug, Sep).
    assert response.json()["total_payslips"] == 5

    await payslip_batches.process(session, tenant.id, uuid.UUID(response.json()["id"]))

    done = (await client.get(f"{BASE}/{response.json()['id']}", headers=bearer(org["hr"]))).json()
    assert done["status"] == "completed"
    assert done["succeeded"] == 3
    sent: dict[str, OutboundMessage] = {m.recipient_email: m for m in mail.sent}
    assert set(sent) == {"e001@acme.test", "e002@acme.test", "e003@acme.test"}
    bina = sent["e002@acme.test"]
    assert bina.subject == "Your payslips for August 2026 to September 2026"
    assert [a.filename for a in bina.attachments] == [
        "payslip-E002-2026-08.pdf",
        "payslip-E002-2026-09.pdf",
    ]
    assert "Acme Technologies Pvt Ltd" in bina.body
    assert bina.html and "Bina Test" in bina.html
    assert [a.filename for a in sent["e001@acme.test"].attachments] == ["payslip-E001-2026-08.pdf"]
    assert sent["e001@acme.test"].subject == "Your payslip for August 2026"

    audits = (
        await session.scalars(select(AuditLog.summary).where(AuditLog.entity_type == "payslip"))
    ).all()
    assert any("of E002 to e002@acme.test" in s for s in audits)
    assert len([s for s in audits if s.startswith("Emailed the")]) == 3


async def test_personal_address_and_password_protection(
    client: httpx.AsyncClient, session: AsyncSession, org, tenant, mail
) -> None:
    tenant.settings = {**(tenant.settings or {}), "payslip_pdf_password": True}
    await session.commit()
    response = await client.post(
        f"{BASE}/email",
        json={
            "employee_ids": [str(org["asha"].id), str(org["bina"].id)],
            "months": 1,
            "to": "2026-08-01",
            "send_to": "personal",
        },
        headers=bearer(org["hr"]),
    )
    assert response.status_code == 202, response.text
    await payslip_batches.process(session, tenant.id, uuid.UUID(response.json()["id"]))
    sent = {m.recipient_email: m for m in mail.sent}
    # Asha has a personal address; Bina has none, so her work address is used.
    assert set(sent) == {"asha.home@mail.test", "e002@acme.test"}
    message = sent["asha.home@mail.test"]
    assert "opens with a password" in message.body
    reader = PdfReader(io.BytesIO(message.attachments[0].data))
    assert reader.is_encrypted
    assert reader.decrypt("WRONG") == 0
    assert reader.decrypt("ABCDE1234F") != 0


async def test_nobody_is_emailed_twice_when_a_batch_is_picked_up_again(
    client: httpx.AsyncClient, session: AsyncSession, org, tenant, mail
) -> None:
    ids = [str(org["bina"].id), str(org["chet"].id)]
    response = await client.post(
        f"{BASE}/email",
        json={"employee_ids": ids, "months": 1, "to": "2026-09-01"},
        headers=bearer(org["hr"]),
    )
    batch = await _batch(session, response.json()["id"])
    # As a restart leaves it: Bina already sent, the batch back in the queue.
    batch.results = [{"employee_id": ids[0], "status": "sent", "payslips": 1}]
    batch.processed = batch.succeeded = 1
    await session.commit()

    await payslip_batches.process(session, tenant.id, batch.id)
    assert [m.recipient_email for m in mail.sent] == ["e003@acme.test"]
    done = await _batch(session, response.json()["id"])
    assert (done.status, done.processed, done.succeeded) == ("completed", 2, 2)

    # A second worker finds nothing to do.
    await payslip_batches.process(session, tenant.id, batch.id)
    assert len(mail.sent) == 1


async def test_people_without_payslips_or_address_are_skipped_and_failures_kept(
    client: httpx.AsyncClient, session: AsyncSession, org, tenant, legal_entity, location, mail
) -> None:
    nobody = await _person(session, tenant, legal_entity, location, "E009", "Nina")
    # Omar is paid by a second company, which has its own September run.
    second = LegalEntity(tenant_id=tenant.id, name="Acme Services LLP", code="ACS")
    session.add(second)
    await session.commit()
    no_mail = await _person(session, tenant, second, location, "E010", "Omar", work_email="")
    await _month(session, tenant, second, [no_mail], 2026, 9)

    class Refusing(ConsoleEmailAdapter):
        async def send(self, message):
            from app.services.notifications.adapters import PermanentFailure

            if message.recipient_email == "e003@acme.test":
                raise PermanentFailure("mailbox does not exist")
            return await super().send(message)

    dispatch.register_adapter(Channel.EMAIL, Refusing())
    ids = [str(nobody.id), str(no_mail.id), str(org["bina"].id), str(org["chet"].id)]
    response = await client.post(
        f"{BASE}/email",
        json={"employee_ids": ids, "months": 1, "to": "2026-09-01"},
        headers=bearer(org["hr"]),
    )
    await payslip_batches.process(session, tenant.id, uuid.UUID(response.json()["id"]))
    done = (await client.get(f"{BASE}/{response.json()['id']}", headers=bearer(org["hr"]))).json()
    assert (done["succeeded"], done["skipped"], done["failed"]) == (1, 2, 1)
    by_code = {r["employee_code"]: r for r in done["results"]}
    assert by_code["E009"]["detail"] == "No payslip released for September 2026."
    assert by_code["E010"]["detail"] == "No work email address on record."
    assert "mailbox does not exist" in by_code["E003"]["detail"]


# ── What is refused ─────────────────────────────────────────────────────────


async def test_what_is_refused(
    client: httpx.AsyncClient, session: AsyncSession, org, other_tenant, tenant
) -> None:
    hr = bearer(org["hr"])
    bina = str(org["bina"].id)

    # Email with no mail server set up would reach nobody.
    no_relay = await client.post(
        f"{BASE}/email", json={"employee_ids": [bina], "months": 1}, headers=hr
    )
    assert no_relay.status_code == 409
    assert "Email is not set up" in no_relay.json()["detail"]

    # A period nobody was paid in.
    empty = await client.post(
        f"{BASE}/export",
        json={"employee_ids": [bina], "months": 1, "to": "2025-01-01"},
        headers=hr,
    )
    assert empty.status_code == 422
    assert "January 2025" in empty.json()["detail"]

    # Only 1, 3, 6 or 12 months.
    odd = await client.post(
        f"{BASE}/export", json={"employee_ids": [bina], "months": 4}, headers=hr
    )
    assert odd.status_code == 422

    # Nobody picked.
    none = await client.post(f"{BASE}/export", json={"employee_ids": [], "months": 1}, headers=hr)
    assert none.status_code == 422

    # Somebody from another workspace.
    stranger = Employee(
        tenant_id=other_tenant.id,
        employee_code="X1",
        first_name="Xi",
        last_name="Other",
        work_email="xi@globex.test",
    )
    session.add(stranger)
    await session.commit()
    foreign = await client.post(
        f"{BASE}/export", json={"employee_ids": [bina, str(stranger.id)], "months": 1}, headers=hr
    )
    assert foreign.status_code == 404

    # An employee cannot use any of it.
    login = User(
        tenant_id=tenant.id,
        email="bina@acme.test",
        password_hash="x",
        full_name="Bina Test",
        role=UserRole.EMPLOYEE,
        is_active=True,
        employee_id=org["bina"].id,
    )
    session.add(login)
    await session.commit()
    assert (
        await client.get(f"{BASE}/people", params={"months": 1}, headers=bearer(login))
    ).status_code == 403
    assert (
        await client.post(
            f"{BASE}/export", json={"employee_ids": [bina], "months": 1}, headers=bearer(login)
        )
    ).status_code == 403


async def test_no_payroll_yet(client: httpx.AsyncClient, hr_user, employee) -> None:
    response = await client.post(
        f"{BASE}/export",
        json={"employee_ids": [str(employee.id)], "months": 1},
        headers=bearer(hr_user),
    )
    assert response.status_code == 409
    people = await client.get(f"{BASE}/people", params={"months": 3}, headers=bearer(hr_user))
    assert people.json()["latest_month"] is None
    assert people.json()["people"][0]["payslips"] == 0


async def test_a_sub_admin_sees_only_their_own_requests(
    client: httpx.AsyncClient, session: AsyncSession, org, tenant
) -> None:
    sub = User(
        tenant_id=tenant.id,
        email="sub@acme.test",
        password_hash="x",
        full_name="Sunil Sub",
        role=UserRole.SUB_ADMIN,
        is_active=True,
    )
    session.add(sub)
    await session.commit()
    bina = [str(org["bina"].id)]
    mine = await client.post(
        f"{BASE}/export", json={"employee_ids": bina, "months": 1}, headers=bearer(sub)
    )
    assert mine.status_code == 202, mine.text
    theirs = await client.post(
        f"{BASE}/export", json={"employee_ids": bina, "months": 3}, headers=bearer(org["hr"])
    )

    listed = await client.get(BASE, headers=bearer(sub))
    assert [b["id"] for b in listed.json()] == [mine.json()["id"]]
    assert listed.json()[0]["requested_by_name"] == "Sunil Sub"
    hidden = await client.get(f"{BASE}/{theirs.json()['id']}", headers=bearer(sub))
    assert hidden.status_code == 404
    admin_view = await client.get(BASE, headers=bearer(org["hr"]))
    assert len(admin_view.json()) == 2


async def test_a_batch_stops_when_its_requester_lost_payroll_access(
    client: httpx.AsyncClient, session: AsyncSession, org, tenant
) -> None:
    response = await client.post(
        f"{BASE}/export",
        json={"employee_ids": [str(org["bina"].id)], "months": 1},
        headers=bearer(org["hr"]),
    )
    org["hr"].role = UserRole.EMPLOYEE
    await session.commit()
    await payslip_batches.process(session, tenant.id, uuid.UUID(response.json()["id"]))
    batch = await _batch(session, response.json()["id"])
    assert batch.status == "failed"
    assert "no longer has payroll access" in batch.error


async def test_the_sweep_picks_up_a_batch_a_restart_cut_off(
    session: AsyncSession, client: httpx.AsyncClient, org, tenant
) -> None:
    response = await client.post(
        f"{BASE}/export",
        json={"employee_ids": [str(org["bina"].id)], "months": 1},
        headers=bearer(org["hr"]),
    )
    batch = await _batch(session, response.json()["id"])
    batch.status = "running"
    batch.heartbeat_at = datetime.now(UTC) - timedelta(minutes=30)
    await session.commit()
    from app.services.scheduler.jobs import system_actor

    outcome = await payslip_batches.sweep(session, system_actor(tenant.id))
    assert outcome["restarted"] == 1
    assert (await _batch(session, response.json()["id"])).status == "queued"
    await payslip_batches.process(session, tenant.id, batch.id)
    assert (await _batch(session, response.json()["id"])).status == "completed"


# ── The payslip on screen ───────────────────────────────────────────────────


async def test_the_payslip_on_screen_is_the_payslip_on_paper(
    client: httpx.AsyncClient, session: AsyncSession, org
) -> None:
    run = await session.scalar(
        select(PayrollRun).where(PayrollRun.period_year == 2026, PayrollRun.period_month == 9)
    )
    slip = await session.scalar(
        select(Payslip).where(Payslip.run_id == run.id, Payslip.employee_id == org["bina"].id)
    )
    body = (
        await client.get(
            f"/api/v1/payroll/runs/{run.id}/payslips/{slip.id}", headers=bearer(org["hr"])
        )
    ).json()
    sheet = body["sheet"]
    assert body["sheet_error"] is None
    assert sheet["company_name"] == "Acme Technologies Pvt Ltd"
    assert sheet["title"] == "Payslip for September 2026"
    assert sheet["facts"][:2] == [
        {"label": "Employee", "value": "Bina Test"},
        {"label": "Employee code", "value": "E002"},
    ]
    assert {"label": "Days paid", "value": "30 of 30"} in sheet["facts"]
    assert [(line["name"], line["amount"]) for line in sheet["earnings"]] == [
        ("BASIC SALARY", "30000.00"),
        ("HOUSE RENT ALLOWANCE", "12000.00"),
    ]
    assert sheet["employer_contributions"] == [
        {"name": "Provident fund (employer)", "amount": "1800.00"}
    ]
    assert sheet["net_pay"] == "40200.00"
    assert sheet["net_pay_in_words"] == "Rupees Forty Thousand Two Hundred Only"

    # A payslip whose lines do not add up shows why instead of a sheet.
    slip.net_pay = Decimal("1.00")
    await session.commit()
    broken = (
        await client.get(
            f"/api/v1/payroll/runs/{run.id}/payslips/{slip.id}", headers=bearer(org["hr"])
        )
    ).json()
    assert broken["sheet"] is None
    assert broken["sheet_error"]


async def test_a_sub_admin_narrowed_to_a_department_reaches_only_its_people(
    client: httpx.AsyncClient, session: AsyncSession, org, tenant
) -> None:
    """A sub-admin whose HR rights cover one department sees and sends only its people."""
    from app.models.access import AdminScope
    from app.models.org import Department

    sales = Department(tenant_id=tenant.id, name="Sales", code="SAL")
    session.add(sales)
    await session.flush()
    org["bina"].department_id = sales.id
    sub = User(
        tenant_id=tenant.id,
        email="scoped@acme.test",
        password_hash="x",
        full_name="Scoped Sub",
        role=UserRole.SUB_ADMIN,
        is_active=True,
    )
    session.add(sub)
    await session.flush()
    session.add(AdminScope(tenant_id=tenant.id, user_id=sub.id, department_ids=[str(sales.id)]))
    await session.commit()

    listed = await client.get(f"{BASE}/people", params={"months": 3}, headers=bearer(sub))
    assert listed.status_code == 200, listed.text
    assert [row["employee_code"] for row in listed.json()["people"]] == ["E002"]

    outside = await client.post(
        f"{BASE}/export",
        json={"employee_ids": [str(org["bina"].id), str(org["chet"].id)], "months": 3},
        headers=bearer(sub),
    )
    assert outside.status_code == 404
    assert "not among the people your HR rights cover" in outside.json()["detail"]

    inside = await client.post(
        f"{BASE}/export",
        json={"employee_ids": [str(org["bina"].id)], "months": 3},
        headers=bearer(sub),
    )
    assert inside.status_code == 202, inside.text
