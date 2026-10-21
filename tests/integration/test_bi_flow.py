"""Slice 37 — the connector and the benchmark against a real database.

The checklist gates:

- connector applies the same sensitivity rules as the report builder
- opt-out client data provably excluded from every benchmark
- minimum sample size enforced on every published cell
- exports audited with full field list
- exposed schema versioned independently of internal tables
- benchmark source labelled

Two of these can only be shown here rather than in the engine tests: that the
consent set is re-read from the database on every aggregation run, and that an
export and its audit row survive as one transaction.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.audit import AuditAction, AuditLog
from app.models.bi import (
    BenchmarkConsent,
    BenchmarkContribution,
    BenchmarkDataset,
    BenchmarkSourceEnum,
    BiConnection,
    BiDestinationKind,
    BiExportKind,
    BiExportRun,
)
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services.bi import aggregation, exposed_schema, published
from app.services.bi import service as bi
from tests.integration.conftest import requires_db

pytestmark = [pytest.mark.asyncio, requires_db]

D = Decimal
NOW = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)
PERIOD = date(2026, 6, 30)


# ── Helpers ─────────────────────────────────────────────────────────────────


async def make_connection(session, user, **kw):
    body = {
        "name": f"warehouse-{uuid.uuid4().hex[:6]}",
        "entity_name": "fact_payslip",
        "destination": BiDestinationKind.OBJECT_STORAGE,
        "destination_ref": "s3://acme-bi/payslips/",
        "export_role": "admin",
    }
    body.update(kw)
    result = await bi.create_connection(session, user, **body)
    row = (
        (
            await session.execute(
                select(BiConnection).where(BiConnection.id == uuid.UUID(result["connection_id"]))
            )
        )
        .scalars()
        .one()
    )
    return row, result


def payslip_rows(count: int, *, start: datetime) -> list[dict]:
    return [
        {
            "employee_code": f"E{index:03d}",
            "department": "Engineering",
            "location": "Bengaluru",
            "gross_pay": D("120000.00"),
            "total_deductions": D("18000.00"),
            "net_pay": D("102000.00"),
            "employer_cost": D("134000.00"),
            "paid_days": 30,
            "lop_days": 0,
            # Not an exposed column. It must not survive the projection.
            "aadhaar": "9999 8888 7777",
            "updated_at": start + timedelta(hours=index),
        }
        for index in range(count)
    ]


async def make_contributor(session, *, code: str) -> tuple[Tenant, User]:
    """A second, third, ... company with an active opt-in."""
    company = Tenant(name=f"Company {code}", slug=f"company-{code.lower()}")
    session.add(company)
    await session.commit()
    await session.refresh(company)

    person = User(
        tenant_id=company.id,
        email=f"hr@{code.lower()}.test",
        password_hash="not-a-real-hash",
        full_name=f"Admin {code}",
        role=UserRole.ADMIN,
        is_active=True,
    )
    session.add(person)
    await session.commit()
    await session.refresh(person)
    return company, person


async def contribute(session, user, *, value, heads=25, metric="median_ctc"):
    await bi.set_consent(session, user, is_contributor=True, contract_reference="MSA-7.4", at=NOW)
    return await bi.record_contribution(
        session,
        user,
        metric=metric,
        dimension_key="saas|200-500|bengaluru|M3",
        period=PERIOD,
        value=D(str(value)),
        employee_count=heads,
    )


# ═══════════════════════════════════════════════════════════════════════════
# The published schema
# ═══════════════════════════════════════════════════════════════════════════


async def test_the_schema_a_role_is_shown_matches_what_it_could_export(session, hr_user):
    """The gate: "connector applies the same sensitivity rules as the report
    builder"."""
    document = bi.schema_for_role(hr_user)
    payslip = next(e for e in document["entities"] if e["name"] == "fact_payslip")
    documented = {c["name"] for c in payslip["columns"]}
    exportable = {
        f.name for f in exposed_schema.visible_columns("fact_payslip", hr_user.role.value)
    }
    assert documented == exportable, "A client is documented a surface they cannot actually export."


async def test_the_schema_document_never_names_an_internal_column(session, hr_user):
    document = bi.schema_for_role(hr_user)
    for entity in document["entities"]:
        for column in entity["columns"]:
            assert "source" not in column


async def test_a_connection_naming_a_raw_table_is_refused(session, hr_user):
    with pytest.raises(HTTPException) as exc:
        await make_connection(session, hr_user, entity_name="employees")
    assert exc.value.status_code == 400
    assert "published schema only" in exc.value.detail


async def test_a_connection_naming_a_field_the_role_cannot_see_is_refused_up_front(
    session, hr_user
):
    """Refused at creation rather than at 2am on the first scheduled run."""
    with pytest.raises(HTTPException) as exc:
        await make_connection(
            session,
            hr_user,
            export_role="employee",
            field_names=["net_pay"],
        )
    assert exc.value.status_code == 400


async def test_a_connection_records_the_schema_version_it_was_built_against(session, hr_user):
    row, result = await make_connection(session, hr_user)
    assert row.schema_version == exposed_schema.SCHEMA_VERSION
    assert result["schema_version"] == exposed_schema.SCHEMA_VERSION


# ═══════════════════════════════════════════════════════════════════════════
# Export
# ═══════════════════════════════════════════════════════════════════════════


async def test_the_first_export_must_be_a_full_one(session, hr_user):
    """A connection with no watermark has nothing to be incremental from, and
    calling it incremental anyway would put the wrong word in the audit row."""
    row, _ = await make_connection(session, hr_user)
    with pytest.raises(HTTPException) as exc:
        await bi.run_export(
            session,
            hr_user,
            connection_id=row.id,
            kind=BiExportKind.INCREMENTAL,
            source_rows=[],
        )
    assert "first run is a full export" in exc.value.detail


async def test_an_export_writes_its_audit_row_with_the_field_list(session, hr_user):
    """The gate: "exports audited with full field list"."""
    row, _ = await make_connection(session, hr_user)
    result = await bi.run_export(
        session,
        hr_user,
        connection_id=row.id,
        kind=BiExportKind.FULL,
        source_rows=payslip_rows(3, start=NOW),
        at=NOW,
    )
    assert result["row_count"] == 3

    run = (
        (
            await session.execute(
                select(BiExportRun).where(BiExportRun.tenant_id == hr_user.tenant_id)
            )
        )
        .scalars()
        .one()
    )
    assert run.field_count == len(run.field_names)
    assert "net_pay" in run.field_names
    assert run.carries_payroll_data is True
    assert run.destination_ref == "s3://acme-bi/payslips/"
    assert run.export_role == "admin"
    assert run.initiated_by_user_id == hr_user.id
    assert run.row_count == 3


async def test_the_export_row_and_the_audit_log_land_together(session, hr_user):
    row, _ = await make_connection(session, hr_user)
    await bi.run_export(
        session,
        hr_user,
        connection_id=row.id,
        kind=BiExportKind.FULL,
        source_rows=payslip_rows(2, start=NOW),
        at=NOW,
    )
    entries = (
        (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.tenant_id == hr_user.tenant_id,
                    AuditLog.action == AuditAction.EXPORT,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(entries) == 1
    assert "net_pay" in entries[0].changes["fields"]


async def test_an_unentitled_column_cannot_leak_through_the_projection(session, hr_user):
    """A source row carrying an Aadhaar number must not reach the file because
    the caller happened to hand one to the exporter."""
    row, _ = await make_connection(session, hr_user)
    result = await bi.run_export(
        session,
        hr_user,
        connection_id=row.id,
        kind=BiExportKind.FULL,
        source_rows=payslip_rows(2, start=NOW),
        at=NOW,
    )
    for exported in result["rows"]:
        assert "aadhaar" not in exported


async def test_a_salary_is_withheld_from_a_role_that_may_not_see_it(session, hr_user):
    """The gate again, this time on the way out rather than in the document."""
    row, created = await make_connection(
        session,
        hr_user,
        export_role="manager",
        field_names=["employee_code", "department", "net_pay"],
    )
    assert created["withheld_fields"] == ["net_pay"]
    result = await bi.run_export(
        session,
        hr_user,
        connection_id=row.id,
        kind=BiExportKind.FULL,
        source_rows=payslip_rows(2, start=NOW),
        at=NOW,
    )
    assert "net_pay" not in result["fields"]
    assert result["withheld_fields"] == ["net_pay"]
    assert result["carries_payroll_data"] is False
    for exported in result["rows"]:
        assert "net_pay" not in exported


async def test_the_second_export_is_a_delta_from_the_stored_watermark(session, hr_user):
    """The gate: "export is incremental and change-tracked"."""
    connection, _ = await make_connection(session, hr_user)
    rows = payslip_rows(3, start=NOW)
    await bi.run_export(
        session,
        hr_user,
        connection_id=connection.id,
        kind=BiExportKind.FULL,
        source_rows=rows,
        at=NOW,
    )
    await session.refresh(connection)
    watermark = connection.last_watermark
    assert watermark == max(r["updated_at"] for r in rows)

    later = payslip_rows(5, start=NOW)
    later[4]["updated_at"] = watermark + timedelta(hours=1)
    result = await bi.run_export(
        session,
        hr_user,
        connection_id=connection.id,
        kind=BiExportKind.INCREMENTAL,
        source_rows=later,
        at=NOW,
    )
    # Only the rows past the watermark.
    assert result["row_count"] == 2
    await session.refresh(connection)
    assert connection.last_watermark == watermark + timedelta(hours=1)


async def test_an_empty_delta_leaves_the_watermark_exactly_where_it_was(session, hr_user):
    """Advancing on an empty run to "now" would skip anything written in
    between, and that row would never be exported again."""
    connection, _ = await make_connection(session, hr_user)
    await bi.run_export(
        session,
        hr_user,
        connection_id=connection.id,
        kind=BiExportKind.FULL,
        source_rows=payslip_rows(2, start=NOW),
        at=NOW,
    )
    await session.refresh(connection)
    before = connection.last_watermark

    await bi.run_export(
        session,
        hr_user,
        connection_id=connection.id,
        kind=BiExportKind.INCREMENTAL,
        source_rows=[],
        at=NOW + timedelta(days=1),
    )
    await session.refresh(connection)
    assert connection.last_watermark == before


async def test_the_export_history_is_readable_with_its_field_lists(session, hr_user):
    connection, _ = await make_connection(session, hr_user)
    await bi.run_export(
        session,
        hr_user,
        connection_id=connection.id,
        kind=BiExportKind.FULL,
        source_rows=payslip_rows(1, start=NOW),
        at=NOW,
    )
    history = await bi.list_export_runs(session, hr_user)
    assert len(history) == 1
    assert history[0]["field_count"] == len(history[0]["fields"])
    assert history[0]["carries_payroll_data"] is True


async def test_another_tenant_cannot_export_through_this_connection(
    session, hr_user, other_hr_user
):
    connection, _ = await make_connection(session, hr_user)
    with pytest.raises(HTTPException) as exc:
        await bi.run_export(
            session,
            other_hr_user,
            connection_id=connection.id,
            kind=BiExportKind.FULL,
            source_rows=[],
        )
    assert exc.value.status_code == 404


async def test_another_tenants_export_history_is_invisible(session, hr_user, other_hr_user):
    connection, _ = await make_connection(session, hr_user)
    await bi.run_export(
        session,
        hr_user,
        connection_id=connection.id,
        kind=BiExportKind.FULL,
        source_rows=payslip_rows(1, start=NOW),
        at=NOW,
    )
    assert await bi.list_export_runs(session, other_hr_user) == []


# ═══════════════════════════════════════════════════════════════════════════
# Consent
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_contribution_without_consent_is_refused(session, hr_user):
    with pytest.raises(HTTPException) as exc:
        await bi.record_contribution(
            session,
            hr_user,
            metric="median_ctc",
            dimension_key="saas|200-500|bengaluru|M3",
            period=PERIOD,
            value=D("1800000"),
            employee_count=40,
        )
    assert exc.value.status_code == 400
    assert "not opted in" in exc.value.detail


async def test_consent_records_who_accepted_and_when(session, hr_user):
    await bi.set_consent(
        session, hr_user, is_contributor=True, contract_reference="MSA-7.4", at=NOW
    )
    row = (
        (
            await session.execute(
                select(BenchmarkConsent).where(BenchmarkConsent.tenant_id == hr_user.tenant_id)
            )
        )
        .scalars()
        .one()
    )
    assert row.is_contributor is True
    assert row.accepted_by_user_id == hr_user.id
    assert row.accepted_at == NOW
    assert row.contract_reference == "MSA-7.4"
    assert row.revoked_at is None


async def test_opting_out_deletes_the_contributions_and_keeps_the_record(session, hr_user):
    """A stored contribution from a client who has withdrawn is a row waiting
    for the next run to forget the flag."""
    await contribute(session, hr_user, value=1800000)
    result = await bi.set_consent(
        session, hr_user, is_contributor=False, at=NOW + timedelta(days=1)
    )
    assert result["contributions_removed"] == 1

    remaining = (
        (
            await session.execute(
                select(BenchmarkContribution).where(
                    BenchmarkContribution.tenant_id == hr_user.tenant_id
                )
            )
        )
        .scalars()
        .all()
    )
    assert remaining == []

    row = (
        (
            await session.execute(
                select(BenchmarkConsent).where(BenchmarkConsent.tenant_id == hr_user.tenant_id)
            )
        )
        .scalars()
        .one()
    )
    assert row.is_contributor is False
    assert row.revoked_at == NOW + timedelta(days=1)
    # The record survives, so "was this client contributing in March" is
    # answerable.
    assert row.accepted_at == NOW


async def test_a_contribution_is_updated_rather_than_duplicated(session, hr_user):
    await contribute(session, hr_user, value=1800000)
    await bi.record_contribution(
        session,
        hr_user,
        metric="median_ctc",
        dimension_key="saas|200-500|bengaluru|M3",
        period=PERIOD,
        value=D("1900000"),
        employee_count=30,
    )
    rows = (
        (
            await session.execute(
                select(BenchmarkContribution).where(
                    BenchmarkContribution.tenant_id == hr_user.tenant_id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].value == D("1900000.00")


# ═══════════════════════════════════════════════════════════════════════════
# Aggregation
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_cell_below_the_contributor_floor_is_stored_unpublished(session, hr_user):
    """The gate: "minimum sample size enforced on every published cell". The
    row exists with its reason, so "why is there no benchmark for my segment"
    has an answer."""
    await contribute(session, hr_user, value=1800000)
    for index in range(3):
        _, person = await make_contributor(session, code=f"C{index}")
        await contribute(session, person, value=1700000 + index * 10000)

    outcome = await aggregation.run_aggregation(session, metric="median_ctc", period=PERIOD, at=NOW)
    assert outcome.contributing_tenants == 4
    assert outcome.cells_published == 0
    assert outcome.cells_suppressed == 1

    cell = await published.fetch_cell(
        session,
        metric="median_ctc",
        dimension_key="saas|200-500|bengaluru|M3",
        period=PERIOD,
        source=BenchmarkSourceEnum.OWN_CLIENT_BASE.value,
    )
    assert cell is not None
    assert cell.published is False
    assert cell.p50 is None
    assert "below the" in cell.note


async def test_a_cell_with_enough_contributors_publishes_quartiles(session, hr_user):
    await contribute(session, hr_user, value=1800000)
    for index in range(9):
        _, person = await make_contributor(session, code=f"D{index}")
        await contribute(session, person, value=1600000 + index * 50000)

    outcome = await aggregation.run_aggregation(session, metric="median_ctc", period=PERIOD, at=NOW)
    assert outcome.contributing_tenants == 10
    assert outcome.cells_published == 1

    cell = await published.fetch_cell(
        session,
        metric="median_ctc",
        dimension_key="saas|200-500|bengaluru|M3",
        period=PERIOD,
        source=BenchmarkSourceEnum.OWN_CLIENT_BASE.value,
    )
    assert cell.published is True
    assert cell.contributors == 10
    assert cell.p25 < cell.p50 < cell.p75
    assert cell.computed_at == NOW


async def test_a_client_who_revokes_disappears_from_the_next_run(session, hr_user):
    """The gate: "opt-out client data provably excluded from every benchmark".

    The consent set is re-read inside the run, so this is a property of the
    database state at aggregation time and not of what a caller passed in.
    """
    await contribute(session, hr_user, value=9000000)
    people = []
    for index in range(9):
        _, person = await make_contributor(session, code=f"E{index}")
        await contribute(session, person, value=1600000 + index * 50000)
        people.append(person)

    await aggregation.run_aggregation(session, metric="median_ctc", period=PERIOD, at=NOW)
    before = await published.fetch_cell(
        session,
        metric="median_ctc",
        dimension_key="saas|200-500|bengaluru|M3",
        period=PERIOD,
        source=BenchmarkSourceEnum.OWN_CLIENT_BASE.value,
    )
    assert before.contributors == 10

    await bi.set_consent(session, hr_user, is_contributor=False, at=NOW + timedelta(days=1))
    await aggregation.run_aggregation(
        session, metric="median_ctc", period=PERIOD, at=NOW + timedelta(days=1)
    )
    after = await published.fetch_cell(
        session,
        metric="median_ctc",
        dimension_key="saas|200-500|bengaluru|M3",
        period=PERIOD,
        source=BenchmarkSourceEnum.OWN_CLIENT_BASE.value,
    )
    assert after.contributors == 9
    # The withdrawing company's 90 lakh figure is gone from the distribution,
    # not merely hidden.
    assert after.p75 < D("9000000")


async def test_an_aggregation_with_no_consent_at_all_writes_nothing(session, hr_user):
    outcome = await aggregation.run_aggregation(session, metric="median_ctc", period=PERIOD, at=NOW)
    assert outcome.contributing_tenants == 0
    assert outcome.cells_written == 0
    rows = (await session.execute(select(BenchmarkDataset))).scalars().all()
    assert rows == []


async def test_a_rerun_replaces_the_cell_rather_than_adding_one(session, hr_user):
    await contribute(session, hr_user, value=1800000)
    for index in range(9):
        _, person = await make_contributor(session, code=f"F{index}")
        await contribute(session, person, value=1600000 + index * 50000)

    await aggregation.run_aggregation(session, metric="median_ctc", period=PERIOD, at=NOW)
    await aggregation.run_aggregation(
        session, metric="median_ctc", period=PERIOD, at=NOW + timedelta(days=1)
    )
    rows = (await session.execute(select(BenchmarkDataset))).scalars().all()
    assert len(rows) == 1
    assert rows[0].computed_at == NOW + timedelta(days=1)


# ═══════════════════════════════════════════════════════════════════════════
# Reading a benchmark
# ═══════════════════════════════════════════════════════════════════════════


async def test_a_non_contributor_is_refused_the_peer_aggregate_and_told_why(session, hr_user):
    with pytest.raises(HTTPException) as exc:
        await bi.benchmark_position(
            session,
            hr_user,
            metric="median_ctc",
            dimension_key="saas|200-500|bengaluru|M3",
            period=PERIOD,
            own_value=D("1750000"),
        )
    assert exc.value.status_code == 400
    assert "contributing clients" in exc.value.detail
    assert "reversible" in exc.value.detail


async def test_a_contributor_gets_a_band_with_the_sample_size_and_the_source(session, hr_user):
    """The gate: "benchmark source labelled" and "percentile position, not just
    an average"."""
    await contribute(session, hr_user, value=1800000)
    for index in range(9):
        _, person = await make_contributor(session, code=f"G{index}")
        await contribute(session, person, value=1600000 + index * 50000)
    await aggregation.run_aggregation(session, metric="median_ctc", period=PERIOD, at=NOW)

    result = await bi.benchmark_position(
        session,
        hr_user,
        metric="median_ctc",
        dimension_key="saas|200-500|bengaluru|M3",
        period=PERIOD,
        own_value=D("1750000"),
    )
    assert result["percentile"] in (25, 50, 75, 100)
    assert result["benchmark"]["contributors"] == 10
    assert result["benchmark"]["source"] == "own_client_base"
    assert "10 contributing" in result["reading"]
    assert result["access"]["is_contributor"] is True


async def test_a_suppressed_cell_yields_no_position_but_says_why(session, hr_user):
    await contribute(session, hr_user, value=1800000)
    for index in range(3):
        _, person = await make_contributor(session, code=f"H{index}")
        await contribute(session, person, value=1700000)
    await aggregation.run_aggregation(session, metric="median_ctc", period=PERIOD, at=NOW)

    result = await bi.benchmark_position(
        session,
        hr_user,
        metric="median_ctc",
        dimension_key="saas|200-500|bengaluru|M3",
        period=PERIOD,
        own_value=D("1750000"),
    )
    assert result["percentile"] is None
    assert "nothing to compare against" in result["reading"]


async def test_a_cell_nobody_has_aggregated_is_a_404(session, hr_user):
    await bi.set_consent(session, hr_user, is_contributor=True, at=NOW)
    with pytest.raises(HTTPException) as exc:
        await bi.benchmark_position(
            session,
            hr_user,
            metric="median_ctc",
            dimension_key="fintech|50-200|pune|M2",
            period=PERIOD,
            own_value=D("1750000"),
        )
    assert exc.value.status_code == 404
