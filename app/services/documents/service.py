"""Letter issuance, reference numbering and bulk generation."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from fastapi import HTTPException, Request, status
from sqlalchemy import desc, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.documents import (
    CATEGORY_CODES,
    BulkLetterJob,
    IssuedLetter,
    LetterCategory,
    LetterSequence,
    LetterStatus,
    LetterTemplate,
)
from app.models.employee import Employee
from app.models.tenant import Tenant
from app.models.user import User
from app.services import audit
from app.services.documents.letter_merge import (
    LetterMergeError,
    MergeContext,
    render_template,
)
from app.services.documents.pdf import Letterhead, content_hash, render_pdf
from app.services.payroll.service import active_salary_on


def financial_year(on: date) -> str:
    """The Indian financial year containing a date, as "2026-27".

    April to March, so a letter issued in February 2027 belongs to 2026-27.
    """
    start = on.year if on.month >= 4 else on.year - 1
    return f"{start}-{str(start + 1)[-2:]}"


async def allocate_reference_number(
    session: AsyncSession, user: User, category: LetterCategory, on: date
) -> str:
    """Allocate the next reference number, e.g. `ACM/OFF/2026-27/00142`.

    Takes a row lock with `SELECT ... FOR UPDATE` so two concurrent issuances
    cannot receive the same number. An application-level counter would fail
    here, and duplicate reference numbers on legal documents are not a
    recoverable error.

    Gaps are acceptable — a rolled-back transaction consumes a number and that
    is fine. Duplicates are not.

    Must be called inside a transaction; the caller commits.
    """
    year = financial_year(on)

    # Create the counter row if it is not there yet. ON CONFLICT DO NOTHING
    # rather than "check then insert": under concurrency every task finds it
    # missing at the same instant, and 49 of 50 plain inserts would then fail
    # on the unique constraint. Letting the database resolve the race is the
    # only version of this that is actually correct.
    await session.execute(
        pg_insert(LetterSequence)
        .values(
            tenant_id=user.tenant_id,
            category=category,
            financial_year=year,
            last_value=0,
        )
        .on_conflict_do_nothing(
            index_elements=["tenant_id", "category", "financial_year"]
        )
    )

    # Now take the row lock. Every concurrent caller queues here, so each one
    # reads a distinct `last_value` — which is what makes the numbers unique.
    sequence = await session.scalar(
        tenant_scoped(
            select(LetterSequence).where(
                LetterSequence.category == category,
                LetterSequence.financial_year == year,
            ),
            LetterSequence,
            user,
        ).with_for_update()
    )
    if sequence is None:  # pragma: no cover - the upsert above guarantees a row
        raise RuntimeError("Letter sequence row vanished between insert and lock")

    sequence.last_value += 1
    number = sequence.last_value

    tenant = await session.get(Tenant, user.tenant_id)
    prefix = (tenant.slug[:3] if tenant else "ASK").upper()
    return f"{prefix}/{CATEGORY_CODES[category]}/{year}/{number:05d}"


async def _build_context(
    session: AsyncSession,
    user: User,
    employee: Employee,
    *,
    as_of: date,
    manual: dict,
) -> MergeContext:
    """Assemble merge sources, resolved as of a date.

    `as_of` matters: a relieving letter issued today for an exit last month must
    show the salary that was in force at exit, not today's.
    """
    tenant = await session.get(Tenant, user.tenant_id)
    salary = await active_salary_on(session, user, employee.id, as_of)

    salary_dict: dict = {}
    components: list[dict] = []
    if salary is not None:
        salary_dict = {
            "ctc_annual": Decimal(str(salary.ctc_annual)),
            "currency": salary.currency,
            "effective_from": salary.effective_from,
        }
        components = [
            {
                "code": c.component_code,
                "name": c.component_name,
                "monthly_amount": Decimal(str(c.monthly_amount)),
                "annual_amount": Decimal(str(c.annual_amount)),
                "part_of_ctc": c.part_of_ctc,
            }
            for c in sorted(salary.components, key=lambda c: c.display_order)
        ]

    return MergeContext(
        as_of=as_of,
        employee={
            "first_name": employee.first_name,
            "last_name": employee.last_name,
            "display_name": employee.display_name,
            "employee_code": employee.employee_code,
            "work_email": employee.work_email,
            "designation": employee.designation,
            "grade": employee.grade,
            "date_of_joining": employee.date_of_joining,
            "date_of_exit": employee.date_of_exit,
            "probation_end_date": employee.probation_end_date,
            "notice_period_days": employee.notice_period_days,
        },
        salary=salary_dict,
        tenant={
            "name": tenant.name if tenant else "",
            "slug": tenant.slug if tenant else "",
        },
        manual=manual,
        salary_components=components,
    )


async def get_template(
    session: AsyncSession, user: User, template_id: uuid.UUID
) -> LetterTemplate:
    template = await session.scalar(
        tenant_scoped(
            select(LetterTemplate).where(LetterTemplate.id == template_id),
            LetterTemplate,
            user,
        )
    )
    if template is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found")
    return template


async def create_template_version(
    session: AsyncSession,
    user: User,
    *,
    code: str,
    payload: dict,
    request: Request | None = None,
) -> LetterTemplate:
    """Create a template, or a new version of an existing one.

    Editing never mutates an existing version. An issued letter names the
    version it used, and that version has to stay exactly as it was — otherwise
    a letter cannot be re-rendered and verified against its stored hash.
    """
    latest = await session.scalar(
        tenant_scoped(
            select(LetterTemplate)
            .where(LetterTemplate.code == code)
            .order_by(desc(LetterTemplate.version)),
            LetterTemplate,
            user,
        )
    )
    next_version = (latest.version + 1) if latest is not None else 1

    if latest is not None:
        # Only one version of a code is active at a time.
        latest.active = False

    template = LetterTemplate(
        tenant_id=user.tenant_id, code=code, version=next_version, **payload
    )
    session.add(template)
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="letter_template",
        entity_id=template.id,
        actor=user,
        changes={"code": code, "version": next_version},
        summary=f"Created letter template {code} v{next_version}",
        request=request,
    )
    await session.commit()
    await session.refresh(template)
    return template


async def issue_letter(
    session: AsyncSession,
    user: User,
    *,
    employee_id: uuid.UUID,
    template_id: uuid.UUID,
    manual_values: dict | None = None,
    as_of: date | None = None,
    generate_pdf: bool = True,
    request: Request | None = None,
    commit: bool = True,
) -> IssuedLetter:
    """Merge, number, render and store one letter.

    Merge and reference allocation happen in the same transaction, so a failed
    merge never consumes a reference number that then appears as a gap with no
    document behind it.
    """
    employee = await session.scalar(
        tenant_scoped(select(Employee).where(Employee.id == employee_id), Employee, user)
    )
    if employee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    template = await get_template(session, user, template_id)
    effective_as_of = as_of or date.today()
    manual = manual_values or {}

    # Required MANUAL fields the template declares must be supplied before we
    # bother merging — a clearer error than "token resolved to nothing".
    missing = [
        f.label
        for f in template.fields
        if f.required and f.source.value == "manual" and not manual.get(f.token.split(".")[-1])
    ]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"These fields are required for this letter: {', '.join(missing)}",
        )

    context = await _build_context(session, user, employee, as_of=effective_as_of, manual=manual)
    required_tokens = {f.token for f in template.fields if f.required}

    try:
        merged = render_template(
            body_html=template.body_html,
            header_html=template.header_html,
            footer_html=template.footer_html,
            context=context,
            required_tokens=required_tokens or None,
        )
    except LetterMergeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    reference = await allocate_reference_number(
        session, user, template.category, effective_as_of
    )

    letter = IssuedLetter(
        tenant_id=user.tenant_id,
        employee_id=employee.id,
        template_id=template.id,
        template_version=template.version,
        category=template.category,
        reference_number=reference,
        merged_html=merged,
        content_hash=content_hash(merged),
        manual_values=_serialise(manual),
        as_of_date=datetime.combine(effective_as_of, datetime.min.time(), tzinfo=UTC),
        status=(
            LetterStatus.PENDING_APPROVAL if template.requires_approval else LetterStatus.APPROVED
        ),
        issued_by_user_id=user.id,
        issued_at=datetime.now(UTC),
    )

    if generate_pdf:
        tenant = await session.get(Tenant, user.tenant_id)
        pdf_bytes = render_pdf(
            merged_html=merged,
            reference_number=reference,
            letterhead=Letterhead(company_name=tenant.name if tenant else "AskMira"),
        )
        # Object storage lands with slice 04's storage work; until then the key
        # is recorded and the bytes are addressable by it.
        letter.pdf_storage_key = f"letters/{user.tenant_id}/{reference.replace('/', '_')}.pdf"
        _PDF_CACHE[letter.pdf_storage_key] = pdf_bytes

    session.add(letter)
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="issued_letter",
        entity_id=letter.id,
        actor=user,
        changes={
            "reference_number": reference,
            "employee_code": employee.employee_code,
            "template": f"{template.code} v{template.version}",
            "content_hash": letter.content_hash,
        },
        summary=f"Issued {template.category.value} letter {reference} to {employee.employee_code}",
        request=request,
    )

    if commit:
        await session.commit()
        await session.refresh(letter)
    return letter


# In-process store for rendered PDFs until object storage lands (slice 04).
# Deliberately not a database column — a PDF in a row is a database nobody can
# back up quickly.
_PDF_CACHE: dict[str, bytes] = {}


def stored_pdf(storage_key: str) -> bytes | None:
    return _PDF_CACHE.get(storage_key)


def _serialise(values: dict) -> dict:
    """JSONB-safe copy of the manual values."""
    out: dict = {}
    for key, value in values.items():
        if isinstance(value, date | datetime):
            out[key] = value.isoformat()
        elif isinstance(value, Decimal):
            out[key] = str(value)
        else:
            out[key] = value
    return out


async def issue_bulk(
    session: AsyncSession,
    user: User,
    *,
    template_id: uuid.UUID,
    employee_ids: list[uuid.UUID],
    manual_values: dict | None = None,
    as_of: date | None = None,
    request: Request | None = None,
) -> BulkLetterJob:
    """Issue one template to many employees.

    Each letter commits on its own. A batch of 200 where employee 137 has no
    salary assigned should produce 199 letters and one reported failure, not
    zero letters — an all-or-nothing batch means one bad record blocks an
    entire increment cycle.
    """
    await get_template(session, user, template_id)

    job = BulkLetterJob(
        tenant_id=user.tenant_id,
        template_id=template_id,
        requested_by_user_id=user.id,
        total=len(employee_ids),
        status="running",
        started_at=datetime.now(UTC),
    )
    session.add(job)
    await session.commit()
    # Hold ids and scalars, not ORM objects. A failed letter below calls
    # rollback(), which expires every instance in this session — touching
    # `job.id` or `user.tenant_id` afterwards would trigger a lazy refresh, and
    # a refresh that lands mid-flush raises MissingGreenlet.
    job_id = job.id
    tenant_id = user.tenant_id
    actor_user_id = user.id

    results: list[dict] = []
    succeeded = 0
    failed = 0

    for employee_id in employee_ids:
        try:
            letter = await issue_letter(
                session,
                user,
                employee_id=employee_id,
                template_id=template_id,
                manual_values=manual_values,
                as_of=as_of,
                request=request,
            )
            results.append(
                {
                    "employee_id": str(employee_id),
                    "reference_number": letter.reference_number,
                    "error": None,
                }
            )
            succeeded += 1
        except HTTPException as exc:
            await session.rollback()
            results.append(
                {"employee_id": str(employee_id), "reference_number": None, "error": exc.detail}
            )
            failed += 1
        except Exception as exc:
            await session.rollback()
            results.append(
                {"employee_id": str(employee_id), "reference_number": None, "error": str(exc)}
            )
            failed += 1

    job = await session.get(BulkLetterJob, job_id)
    job.results = results
    job.succeeded = succeeded
    job.failed = failed
    job.status = "completed"
    job.completed_at = datetime.now(UTC)

    await audit.record(
        session,
        tenant_id=tenant_id,
        action=AuditAction.CREATE,
        entity_type="bulk_letter_job",
        entity_id=job_id,
        actor_user_id=actor_user_id,
        changes={"total": len(employee_ids), "succeeded": succeeded, "failed": failed},
        summary=f"Bulk letter run: {succeeded} issued, {failed} failed",
        request=request,
    )
    await session.commit()
    await session.refresh(job)
    return job


async def verify_letter(
    session: AsyncSession, user: User, letter_id: uuid.UUID
) -> tuple[IssuedLetter, bool]:
    """Confirm a stored letter's content still matches its hash.

    Detects tampering with `merged_html` after issuance.
    """
    letter = await session.scalar(
        tenant_scoped(select(IssuedLetter).where(IssuedLetter.id == letter_id), IssuedLetter, user)
    )
    if letter is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Letter not found")
    return letter, content_hash(letter.merged_html) == letter.content_hash
