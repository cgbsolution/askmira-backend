"""Letter templates, issuance, reference numbering, signature and storage.

Issued-letter PDFs live in the storage adapter (`app.services.storage`), not in
process memory — a restart used to lose every rendered letter. A letter whose
stored file has gone missing is re-rendered from its hash-verified merged HTML,
which the PDF renderer turns into byte-identical output.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import desc, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.config import settings
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.documents import (
    CATEGORY_CODES,
    BulkLetterJob,
    FieldSource,
    IssuedLetter,
    LetterCategory,
    LetterSequence,
    LetterStatus,
    LetterTemplate,
    LetterTemplateField,
)
from app.models.employee import Employee
from app.models.hiring import Candidate
from app.models.letter_acknowledgement import LetterAcknowledgement
from app.models.notifications import Channel, DigestMode
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.schemas.letters import (
    DraftPreviewRequest,
    LetterContentRead,
    LetterRead,
    MergeFieldsRead,
    PreviewRead,
    PreviewRequest,
    SignatureProviderRead,
    TemplateFieldInput,
    TemplateUpdate,
    VerifyRead,
)
from app.services import audit
from app.services.documents.letter_merge import (
    AVAILABLE_FIELDS,
    COMPUTED_HANDLERS,
    LetterMergeError,
    MergeContext,
    preview_render,
    render_template,
    template_token_problems,
)
from app.services.documents.pdf import Letterhead, PdfRenderError, content_hash, render_pdf
from app.services.documents.seed_templates import REVIEW_BANNER, SEED_TEMPLATES
from app.services.documents.signature import SignatureError, get_provider
from app.services.notifications.dispatch import notify, register_event
from app.services.payroll.service import active_salary_on
from app.services.storage import StorageError, get_storage

#: Letters an employee may open and acknowledge in the portal. A letter still
#: held for approval has not been issued to anybody yet; a revoked one has
#: been withdrawn.
ACKNOWLEDGEABLE = frozenset({LetterStatus.APPROVED, LetterStatus.SENT, LetterStatus.SIGNED})

_PROMPT = {Channel.IN_APP: DigestMode.IMMEDIATE, Channel.EMAIL: DigestMode.IMMEDIATE}
register_event(
    "documents.letter.issued",
    "Your {letter_name} is ready",
    "Your {letter_name} ({reference_number}) has been issued. Open Documents to read it "
    "and acknowledge it.",
    preferences=_PROMPT,
)
register_event(
    "documents.letter.acknowledgement_requested",
    "Please acknowledge your {letter_name}",
    "{actor_name} asked you to read and acknowledge your {letter_name} "
    "({reference_number}). Open Documents to do it.",
    preferences=_PROMPT,
)
register_event(
    "documents.letter.acknowledged",
    "{employee_name} acknowledged {reference_number}",
    "{employee_name} read and acknowledged their {letter_name} ({reference_number}).",
)


async def notify_letter_issued(
    session: AsyncSession, user: User, letter: IssuedLetter, letter_name: str
) -> None:
    """Tell the employee a letter is waiting for them, in the caller's transaction.

    Only once it is really issued — approved, not held for approval — and only
    to an employee with a login. An offer letter to a candidate goes through the
    offer, not here.
    """
    if letter.employee_id is None or letter.status not in ACKNOWLEDGEABLE:
        return
    recipients = list(
        (
            await session.scalars(
                tenant_scoped(
                    select(User.id).where(
                        User.employee_id == letter.employee_id, User.is_active.is_(True)
                    ),
                    User,
                    user,
                )
            )
        ).all()
    )
    if not recipients:
        return
    await notify(
        session,
        tenant_id=user.tenant_id,
        event_key="documents.letter.issued",
        recipient_user_ids=recipients,
        payload={"letter_name": letter_name, "reference_number": letter.reference_number},
        commit=False,
    )


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
        .on_conflict_do_nothing(index_elements=["tenant_id", "category", "financial_year"])
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


# ═══════════════════════════════════════════════════════════════════════════
# Merge context
# ═══════════════════════════════════════════════════════════════════════════


def _employee_values(employee: Employee) -> dict[str, Any]:
    """Exactly the keys `AVAILABLE_FIELDS["employee"]` promises."""
    return {key: getattr(employee, key) for key in AVAILABLE_FIELDS["employee"]}


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

    statutory, nominees, family = await _statutory_values(session, user, employee)
    return MergeContext(
        as_of=as_of,
        employee=_employee_values(employee),
        salary=salary_dict,
        tenant={
            "name": tenant.name if tenant else "",
            "slug": tenant.slug if tenant else "",
        },
        manual=manual,
        salary_components=components,
        statutory=statutory,
        nominees=nominees,
        family=family,
    )


def _address_line(address: dict | None) -> str | None:
    if not address:
        return None
    parts = [
        address.get("line1"),
        address.get("line2"),
        address.get("city"),
        address.get("district"),
        address.get("state_code"),
        address.get("pincode"),
    ]
    return ", ".join(str(p) for p in parts if p)


async def _statutory_values(
    session: AsyncSession, user: User, employee: Employee
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Exactly `AVAILABLE_FIELDS["statutory"]`, plus the nominee and family lists."""
    from app.models.employee import EmployeeProfile

    profile = await session.scalar(
        tenant_scoped(
            select(EmployeeProfile).where(EmployeeProfile.employee_id == employee.id),
            EmployeeProfile,
            user,
        )
    )
    personal = (profile.personal if profile else None) or {}
    addresses = (profile.addresses if profile else None) or {}

    def _parse(value: Any) -> Any:
        if isinstance(value, str) and len(value) == 10 and value[4] == "-":
            with contextlib.suppress(ValueError):
                return date.fromisoformat(value)
        return value

    values = {
        "date_of_birth": employee.date_of_birth,
        "gender": employee.gender.value.replace("_", " ").capitalize() if employee.gender else None,
        "father_or_spouse_name": personal.get("father_name") or personal.get("spouse_name"),
        "marital_status": str(personal.get("marital_status") or "").replace("_", " ").capitalize()
        or None,
        "mobile": employee.phone,
        "email": employee.personal_email or employee.work_email,
        "uan": employee.uan,
        "pf_number": employee.pf_number,
        "esi_number": employee.esi_number,
        "previous_pf_member": bool(employee.uan),
        "current_address": _address_line(addresses.get("current")),
        "permanent_address": _address_line(addresses.get("permanent"))
        or _address_line(addresses.get("current")),
    }
    nominees = [
        {key: _parse(value) for key, value in item.items()}
        for item in ((profile.nominees if profile else None) or [])
    ]
    family = [
        {key: _parse(value) for key, value in item.items()}
        for item in ((profile.family_members if profile else None) or [])
    ]
    return values, nominees, family


def _sample_context(tenant: Tenant | None, *, as_of: date, manual: dict) -> MergeContext:
    """Placeholders for a preview with nobody chosen.

    Every employee value is its own label in brackets, so nothing in the preview
    can be mistaken for a real person's data. There is no salary: salary tokens
    show as holes until a real employee is picked.
    """
    employee = {key: f"[{key.replace('_', ' ')}]" for key in AVAILABLE_FIELDS["employee"]}
    for key in ("date_of_joining", "date_of_exit", "probation_end_date", "notice_period_days"):
        employee[key] = None
    statutory = {key: f"[{key.replace('_', ' ')}]" for key in AVAILABLE_FIELDS["statutory"]}
    return MergeContext(
        as_of=as_of,
        employee=employee,
        salary={},
        tenant={"name": tenant.name if tenant else "", "slug": tenant.slug if tenant else ""},
        manual=manual,
        statutory=statutory,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Templates
# ═══════════════════════════════════════════════════════════════════════════


async def get_template(session: AsyncSession, user: User, template_id: uuid.UUID) -> LetterTemplate:
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


async def list_templates(
    session: AsyncSession, user: User, *, include_inactive: bool = False
) -> list[LetterTemplate]:
    """Active versions for everybody; every version for HR when asked."""
    stmt = select(LetterTemplate).order_by(
        LetterTemplate.category, LetterTemplate.code, desc(LetterTemplate.version)
    )
    if not (include_inactive and permissions.is_hr(user)):
        stmt = stmt.where(LetterTemplate.active.is_(True))
    return list((await session.scalars(tenant_scoped(stmt, LetterTemplate, user))).all())


async def template_versions(
    session: AsyncSession, user: User, template_id: uuid.UUID
) -> list[LetterTemplate]:
    template = await get_template(session, user, template_id)
    stmt = tenant_scoped(
        select(LetterTemplate)
        .where(LetterTemplate.code == template.code)
        .order_by(desc(LetterTemplate.version)),
        LetterTemplate,
        user,
    )
    return list((await session.scalars(stmt)).all())


_SOURCE_PREFIX = {
    FieldSource.EMPLOYEE: "employee",
    FieldSource.SALARY: "salary",
    FieldSource.TENANT: "tenant",
    FieldSource.MANUAL: "manual",
    FieldSource.COMPUTED: "computed",
}


def _content_problems(
    *,
    body_html: str,
    header_html: str | None,
    footer_html: str | None,
    fields: list[TemplateFieldInput] | list[dict],
) -> list[str]:
    problems = template_token_problems(header_html, body_html, footer_html)
    seen: set[str] = set()
    for item in fields:
        field = item if isinstance(item, TemplateFieldInput) else TemplateFieldInput(**item)
        if field.token in seen:
            problems.append(f"'{field.token}' is declared twice")
        seen.add(field.token)
        if field.token.partition(".")[0] != _SOURCE_PREFIX[field.source]:
            problems.append(
                f"'{field.token}' is declared as a {field.source.value} field but its token "
                f"starts with '{field.token.partition('.')[0]}'"
            )
        problems.extend(template_token_problems("{{" + field.token + "}}"))
    return problems


async def create_template_version(
    session: AsyncSession,
    user: User,
    *,
    code: str,
    payload: dict,
    fields: list[TemplateFieldInput] | list[dict] | None = None,
    request: Request | None = None,
) -> LetterTemplate:
    """Create a template, or a new version of an existing one.

    Editing never mutates an existing version. An issued letter names the
    version it used, and that version has to stay exactly as it was — otherwise
    a letter cannot be re-rendered and verified against its stored hash.

    The template and its declared fields land in one transaction with the audit
    entry. A token that can never resolve is refused here, at authoring time.
    """
    field_items = list(fields or [])
    problems = _content_problems(
        body_html=payload["body_html"],
        header_html=payload.get("header_html"),
        footer_html=payload.get("footer_html"),
        fields=field_items,
    )
    if problems:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="This template cannot be saved: " + "; ".join(problems),
        )

    latest = await session.scalar(
        tenant_scoped(
            select(LetterTemplate)
            .where(LetterTemplate.code == code)
            .order_by(desc(LetterTemplate.version))
            .with_for_update(),
            LetterTemplate,
            user,
        )
    )
    next_version = (latest.version + 1) if latest is not None else 1

    if latest is not None:
        # Only one version of a code is active at a time.
        latest.active = False
        for other in await _active_versions(session, user, code):
            other.active = False

    content = {k: v for k, v in payload.items() if k not in {"code", "fields"}}
    template = LetterTemplate(tenant_id=user.tenant_id, code=code, version=next_version, **content)
    for item in field_items:
        field = item if isinstance(item, TemplateFieldInput) else TemplateFieldInput(**item)
        template.fields.append(LetterTemplateField(tenant_id=user.tenant_id, **field.model_dump()))
    session.add(template)
    await session.flush()

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="letter_template",
        entity_id=template.id,
        actor=user,
        changes={"code": code, "version": next_version, "fields": len(field_items)},
        summary=f"Created letter template {code} v{next_version}",
        request=request,
    )
    await session.commit()
    await session.refresh(template)
    return template


async def _active_versions(session: AsyncSession, user: User, code: str) -> list[LetterTemplate]:
    stmt = tenant_scoped(
        select(LetterTemplate).where(LetterTemplate.code == code, LetterTemplate.active.is_(True)),
        LetterTemplate,
        user,
    )
    return list((await session.scalars(stmt)).all())


async def update_template(
    session: AsyncSession,
    user: User,
    template_id: uuid.UUID,
    payload: TemplateUpdate,
    *,
    request: Request | None = None,
) -> LetterTemplate:
    """An edit is the next version of the same code. The edited row is untouched."""
    template = await get_template(session, user, template_id)
    return await create_template_version(
        session,
        user,
        code=template.code,
        payload=payload.model_dump(exclude={"fields"}),
        fields=payload.fields,
        request=request,
    )


async def seed_templates(
    session: AsyncSession, user: User, *, request: Request | None = None
) -> list[LetterTemplate]:
    """Install the standard Indian-format seed templates, skipping codes in use."""
    created: list[LetterTemplate] = []
    for spec in SEED_TEMPLATES:
        existing = await session.scalar(
            tenant_scoped(
                select(LetterTemplate.id).where(LetterTemplate.code == spec["code"]),
                LetterTemplate,
                user,
            )
        )
        if existing is not None:
            continue

        template = LetterTemplate(
            tenant_id=user.tenant_id,
            code=spec["code"],
            name=spec["name"],
            category=spec["category"],
            body_html=spec["body_html"],
            footer_html=f'<p style="font-size:8pt;color:#6b788d">{REVIEW_BANNER}</p>',
            version=1,
            active=True,
            is_seed=True,
            requires_approval=spec.get("requires_approval", False),
            approver_roles=spec.get("approver_roles", []),
            requires_signature=spec.get("requires_signature", False),
            signatory_role=spec.get("signatory_role"),
        )
        for item in spec["fields"]:
            template.fields.append(LetterTemplateField(tenant_id=user.tenant_id, **item))
        session.add(template)
        created.append(template)

    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="letter_template",
        actor=user,
        summary=f"Installed {len(created)} seed letter template(s)",
        request=request,
    )
    await session.commit()
    for template in created:
        await session.refresh(template)
    return created


def merge_fields() -> MergeFieldsRead:
    return MergeFieldsRead(
        sources={name: list(fields) for name, fields in AVAILABLE_FIELDS.items()},
        computed=sorted(COMPUTED_HANDLERS),
        manual_note=(
            "manual.<anything> is typed by the person issuing the letter. Declare it as a "
            "required manual field so issuance prompts for it."
        ),
    )


# ── Preview ─────────────────────────────────────────────────────────────────


async def _preview(
    session: AsyncSession,
    user: User,
    *,
    body_html: str,
    header_html: str | None,
    footer_html: str | None,
    fields: list[Any],
    request: PreviewRequest,
) -> PreviewRead:
    as_of = request.as_of or date.today()
    manual = dict(request.manual_values)

    if request.employee_id is not None:
        employee = await _employee(session, user, request.employee_id)
        context = await _build_context(session, user, employee, as_of=as_of, manual=manual)
        label: str | None = f"{employee.full_name} ({employee.employee_code})"
    else:
        tenant = await session.get(Tenant, user.tenant_id)
        context = _sample_context(tenant, as_of=as_of, manual=manual)
        label = None

    required = {f.token for f in fields if f.required}
    html_out, problems = preview_render(
        body_html=body_html,
        header_html=header_html,
        footer_html=footer_html,
        context=context,
        required_tokens=required or None,
    )
    for field in fields:
        if field.required and field.source == FieldSource.MANUAL:
            key = field.token.split(".")[-1]
            if not manual.get(key):
                problems.append(f"{field.token}: '{field.label}' must be typed in at issuance")

    return PreviewRead(
        html=html_out,
        problems=sorted(set(problems)),
        is_sample=request.employee_id is None,
        employee_label=label,
        as_of=as_of,
    )


async def preview_template(
    session: AsyncSession, user: User, template_id: uuid.UUID, request: PreviewRequest
) -> PreviewRead:
    """Render a saved version. No letter row, no reference number, no audit."""
    template = await get_template(session, user, template_id)
    return await _preview(
        session,
        user,
        body_html=template.body_html,
        header_html=template.header_html,
        footer_html=template.footer_html,
        fields=list(template.fields),
        request=request,
    )


async def preview_draft(
    session: AsyncSession, user: User, request: DraftPreviewRequest
) -> PreviewRead:
    """Render unsaved content, so an author sees the holes before saving."""
    problems = _content_problems(
        body_html=request.body_html,
        header_html=request.header_html,
        footer_html=request.footer_html,
        fields=request.fields,
    )
    result = await _preview(
        session,
        user,
        body_html=request.body_html,
        header_html=request.header_html,
        footer_html=request.footer_html,
        fields=request.fields,
        request=request,
    )
    result.problems = sorted(set(result.problems) | set(problems))
    return result


# ═══════════════════════════════════════════════════════════════════════════
# Issuance
# ═══════════════════════════════════════════════════════════════════════════


async def _employee(session: AsyncSession, user: User, employee_id: uuid.UUID) -> Employee:
    employee = await session.scalar(
        tenant_scoped(select(Employee).where(Employee.id == employee_id), Employee, user)
    )
    if employee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")
    return employee


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

    With `commit=False` the caller owns the transaction — the lifecycle cascade
    issues its letter inside its own savepoint. A stored PDF whose row is then
    rolled back is left as an unreferenced file under a random key; it is never
    addressable through the API.
    """
    employee = await _employee(session, user, employee_id)

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

    reference = await allocate_reference_number(session, user, template.category, effective_as_of)

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

    stored_key: str | None = None
    if generate_pdf:
        tenant = await session.get(Tenant, user.tenant_id)
        try:
            pdf_bytes = render_pdf(
                merged_html=merged,
                reference_number=reference,
                letterhead=Letterhead(company_name=tenant.name if tenant else "AskMira"),
            )
        except PdfRenderError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc
        storage = get_storage()
        stored_key = storage.new_key(user.tenant_id, "letters", ".pdf")
        try:
            await storage.put(stored_key, pdf_bytes)
        except StorageError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
            ) from exc
        letter.pdf_storage_key = stored_key

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
    await notify_letter_issued(session, user, letter, template.name)

    if commit:
        try:
            await session.commit()
        except BaseException:
            if stored_key:
                with contextlib.suppress(StorageError):
                    await get_storage().delete(stored_key)
            raise
        await session.refresh(letter)
    return letter


async def stored_pdf(storage_key: str) -> bytes | None:
    """The stored bytes at a key, or None."""
    if not storage_key:
        return None
    try:
        return await get_storage().get(storage_key)
    except StorageError:
        return None


async def _pdf_bytes(session: AsyncSession, letter: IssuedLetter) -> bytes:
    """The letter's PDF: stored, or re-rendered from verified content.

    Only called after the content hash has been checked. A re-render of intact
    content is byte-identical to the original, so writing it back at the same
    key restores the stored document rather than changing it.
    """
    data = await stored_pdf(letter.pdf_storage_key or "")
    if data is not None:
        return data

    tenant = await session.get(Tenant, letter.tenant_id)
    try:
        data = render_pdf(
            merged_html=letter.merged_html,
            reference_number=letter.reference_number,
            letterhead=Letterhead(company_name=tenant.name if tenant else "AskMira"),
        )
    except PdfRenderError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if letter.pdf_storage_key:
        with contextlib.suppress(StorageError):
            await get_storage().put(letter.pdf_storage_key, data)
    return data


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


# ═══════════════════════════════════════════════════════════════════════════
# Reading issued letters
# ═══════════════════════════════════════════════════════════════════════════


def _ensure_reader(letter: IssuedLetter, *, is_hr: bool, own_employee_id: uuid.UUID | None) -> None:
    """A letter the caller may open: HR, or the employee it was issued to.

    Anyone else is told it does not exist. An offer letter to a candidate is
    HR's only.
    """
    if not is_hr and (letter.employee_id is None or letter.employee_id != own_employee_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Letter not found")


async def _letter_reads(
    session: AsyncSession, user: User, letters: list[IssuedLetter]
) -> list[LetterRead]:
    template_ids = {letter.template_id for letter in letters}
    employee_ids = {letter.employee_id for letter in letters if letter.employee_id}
    candidate_ids = {letter.candidate_id for letter in letters if letter.candidate_id}

    templates: dict[uuid.UUID, str] = {}
    if template_ids:
        stmt = tenant_scoped(
            select(LetterTemplate.id, LetterTemplate.name).where(
                LetterTemplate.id.in_(template_ids)
            ),
            LetterTemplate,
            user,
        )
        templates = {row.id: row.name for row in (await session.execute(stmt)).all()}
    names: dict[uuid.UUID, str] = {}
    if employee_ids:
        stmt = tenant_scoped(select(Employee).where(Employee.id.in_(employee_ids)), Employee, user)
        names |= {
            e.id: f"{e.full_name} ({e.employee_code})" for e in (await session.scalars(stmt)).all()
        }
    if candidate_ids:
        stmt = tenant_scoped(
            select(Candidate.id, Candidate.full_name).where(Candidate.id.in_(candidate_ids)),
            Candidate,
            user,
        )
        names |= {
            row.id: f"{row.full_name} (candidate)" for row in (await session.execute(stmt)).all()
        }
    acks: dict[uuid.UUID, LetterAcknowledgement] = {}
    if letters:
        stmt = tenant_scoped(
            select(LetterAcknowledgement).where(
                LetterAcknowledgement.letter_id.in_([letter.id for letter in letters])
            ),
            LetterAcknowledgement,
            user,
        )
        acks = {row.letter_id: row for row in (await session.scalars(stmt)).all()}

    def _ack(letter: IssuedLetter) -> dict[str, Any]:
        row = acks.get(letter.id)
        acknowledged = row is not None and row.acknowledged_at is not None
        return {
            "acknowledged_at": row.acknowledged_at if row else None,
            "acknowledged_name": row.typed_name if row else None,
            "acknowledgement_requested_at": row.requested_at if row else None,
            "can_acknowledge": (
                not acknowledged
                and letter.employee_id is not None
                and letter.employee_id == user.employee_id
                and letter.status in ACKNOWLEDGEABLE
            ),
        }

    return [
        LetterRead(
            id=letter.id,
            employee_id=letter.employee_id,
            candidate_id=letter.candidate_id,
            recipient_name=names.get(letter.employee_id or letter.candidate_id, "Unknown"),
            template_id=letter.template_id,
            template_name=templates.get(letter.template_id, "Deleted template"),
            template_version=letter.template_version,
            category=letter.category,
            reference_number=letter.reference_number,
            content_hash=letter.content_hash,
            status=letter.status,
            issued_at=letter.issued_at,
            approved_at=letter.approved_at,
            signed_at=letter.signed_at,
            signature_provider=letter.signature_provider,
            has_pdf=True,
            has_signed_pdf=bool(letter.signed_pdf_storage_key),
            revoked_reason=letter.revoked_reason,
            **_ack(letter),
        )
        for letter in letters
    ]


async def letter_reads(
    session: AsyncSession, user: User, letters: list[IssuedLetter]
) -> list[LetterRead]:
    """The read shape of letters the caller has already been allowed to see."""
    return await _letter_reads(session, user, letters)


async def list_letters(
    session: AsyncSession,
    user: User,
    *,
    employee_id: uuid.UUID | None = None,
    letter_status: LetterStatus | None = None,
    category: LetterCategory | None = None,
    limit: int = 500,
) -> list[LetterRead]:
    """An employee's archive, or — for HR — every letter in the tenant."""
    stmt = tenant_scoped(
        select(IssuedLetter).order_by(IssuedLetter.issued_at.desc()).limit(limit),
        IssuedLetter,
        user,
    )
    if not permissions.is_hr(user):
        # An issued letter is somebody's offer, appointment or relieving
        # letter, usually with their salary in it.
        own = permissions.resolve_employee(
            user, employee_id, detail="You can only see your own letters."
        )
        stmt = stmt.where(IssuedLetter.employee_id == own)
    elif employee_id:
        stmt = stmt.where(IssuedLetter.employee_id == employee_id)
    if letter_status is not None:
        stmt = stmt.where(IssuedLetter.status == letter_status)
    if category is not None:
        stmt = stmt.where(IssuedLetter.category == category)
    return await _letter_reads(session, user, list((await session.scalars(stmt)).all()))


async def letter_content(
    session: AsyncSession, user: User, letter_id: uuid.UUID
) -> LetterContentRead:
    letter, intact = await verify_letter(session, user, letter_id)
    _ensure_reader(letter, is_hr=permissions.is_hr(user), own_employee_id=user.employee_id)
    return LetterContentRead(
        reference_number=letter.reference_number, merged_html=letter.merged_html, intact=intact
    )


async def letter_verify(session: AsyncSession, user: User, letter_id: uuid.UUID) -> VerifyRead:
    letter, intact = await verify_letter(session, user, letter_id)
    _ensure_reader(letter, is_hr=permissions.is_hr(user), own_employee_id=user.employee_id)
    return VerifyRead(
        reference_number=letter.reference_number, content_hash=letter.content_hash, intact=intact
    )


async def letter_pdf(
    session: AsyncSession, user: User, letter_id: uuid.UUID, *, signed: bool = False
) -> tuple[str, bytes]:
    """The PDF and a safe filename. Refused outright if the content was tampered with."""
    letter, intact = await verify_letter(session, user, letter_id)
    _ensure_reader(letter, is_hr=permissions.is_hr(user), own_employee_id=user.employee_id)
    if not intact:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Letter {letter.reference_number} does not match its stored content hash. "
                "It may have been tampered with; do not distribute it."
            ),
        )
    stem = letter.reference_number.replace("/", "_")
    if signed:
        data = await stored_pdf(letter.signed_pdf_storage_key or "")
        if data is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No signed copy of this letter has been received.",
            )
        return f"{stem}-signed.pdf", data
    return f"{stem}.pdf", await _pdf_bytes(session, letter)


# ═══════════════════════════════════════════════════════════════════════════
# Approval, revocation, signature
# ═══════════════════════════════════════════════════════════════════════════


async def _letter_for_hr(session: AsyncSession, user: User, letter_id: uuid.UUID) -> IssuedLetter:
    permissions.require_hr(user, detail="Only HR can act on issued letters.")
    letter, _ = await verify_letter(session, user, letter_id)
    return letter


async def approve_letter(
    session: AsyncSession, user: User, letter_id: uuid.UUID, *, request: Request | None = None
) -> LetterRead:
    """Release a letter held for approval.

    Nobody approves a letter they issued, nor one addressed to themselves, and
    where the template names approver roles the approver must hold one.
    """
    letter = await _letter_for_hr(session, user, letter_id)
    if letter.status is not LetterStatus.PENDING_APPROVAL:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Letter {letter.reference_number} is {letter.status.value}, not awaiting approval."
            ),
        )
    if letter.issued_by_user_id == user.id:
        raise permissions.forbidden("You issued this letter, so somebody else has to approve it.")
    if letter.employee_id is not None and letter.employee_id == user.employee_id:
        raise permissions.forbidden("A letter addressed to you needs somebody else's approval.")
    template = await get_template(session, user, letter.template_id)
    if (
        template.approver_roles
        and user.role.value not in template.approver_roles
        and (user.role is not UserRole.SUPER_ADMIN)
    ):
        raise permissions.forbidden(
            "This template names who may approve it: "
            + ", ".join(role.replace("_", " ") for role in template.approver_roles)
            + "."
        )

    letter.status = LetterStatus.APPROVED
    letter.approved_by_user_id = user.id
    letter.approved_at = datetime.now(UTC)
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.APPROVE,
        entity_type="issued_letter",
        entity_id=letter.id,
        actor=user,
        summary=f"Approved letter {letter.reference_number}",
        request=request,
    )
    await notify_letter_issued(session, user, letter, template.name)
    await session.commit()
    await session.refresh(letter)
    return (await _letter_reads(session, user, [letter]))[0]


async def revoke_letter(
    session: AsyncSession,
    user: User,
    letter_id: uuid.UUID,
    *,
    reason: str,
    request: Request | None = None,
) -> LetterRead:
    """Withdraw a letter. Status only: its content and PDF stay as they were."""
    letter = await _letter_for_hr(session, user, letter_id)
    if letter.status is LetterStatus.REVOKED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="This letter is already revoked."
        )
    if letter.employee_id is not None and letter.employee_id == user.employee_id:
        raise permissions.forbidden("A letter addressed to you is revoked by somebody else.")
    previous = letter.status.value
    letter.status = LetterStatus.REVOKED
    letter.revoked_reason = reason.strip()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="issued_letter",
        entity_id=letter.id,
        actor=user,
        changes={"status": {"from": previous, "to": "revoked"}, "reason": letter.revoked_reason},
        summary=f"Revoked letter {letter.reference_number}",
        request=request,
    )
    await session.commit()
    await session.refresh(letter)
    return (await _letter_reads(session, user, [letter]))[0]


def build_provider(name: str):
    """Construct a provider from configuration.

    Production credentials come from settings; the noop provider is only ever
    selectable outside production.
    """
    if name == "noop":
        if settings.is_production:
            raise SignatureError("The noop signature provider is not available in production")
        return get_provider("noop", webhook_secret=settings.JWT_SECRET_KEY)

    if name == "digio":
        return get_provider(
            "digio",
            client_id=settings.DIGIO_CLIENT_ID or "",
            client_secret=settings.DIGIO_CLIENT_SECRET or "",
            webhook_secret=settings.DIGIO_WEBHOOK_SECRET or "",
        )

    raise SignatureError(f"Unknown signature provider '{name}'")


def signature_provider_status() -> SignatureProviderRead:
    """Whether letters can really be e-signed on this deployment. Said plainly."""
    name = (settings.SIGNATURE_PROVIDER or "noop").lower()
    if name == "noop":
        return SignatureProviderRead(
            provider="noop",
            connected=False,
            detail=(
                "E-signature provider not connected. Letters can be issued and downloaded, "
                "but sending for signature uses a development stand-in that signs nothing."
            ),
        )
    if name == "digio":
        if not (settings.DIGIO_CLIENT_ID and settings.DIGIO_CLIENT_SECRET):
            return SignatureProviderRead(
                provider="digio",
                connected=False,
                detail="Digio is selected but its credentials are not configured.",
            )
        return SignatureProviderRead(
            provider="digio",
            connected=True,
            detail=(
                "Digio credentials are configured. The adapter has not yet been verified "
                "against a live Digio account."
            ),
        )
    return SignatureProviderRead(
        provider=name, connected=False, detail=f"'{name}' is not a supported provider."
    )


async def send_for_signature(
    session: AsyncSession,
    user: User,
    letter_id: uuid.UUID,
    *,
    provider_name: str | None = None,
    request: Request | None = None,
) -> LetterRead:
    letter = await _letter_for_hr(session, user, letter_id)
    if letter.status not in {LetterStatus.APPROVED, LetterStatus.SENT}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A letter in status '{letter.status.value}' cannot be sent for signature",
        )
    if letter.employee_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Offer letters are sent for signature from the offer, not from here.",
        )
    if content_hash(letter.merged_html) != letter.content_hash:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This letter does not match its stored content hash and cannot be sent.",
        )

    document = await _pdf_bytes(session, letter)
    employee = await _employee(session, user, letter.employee_id)

    try:
        provider = build_provider((provider_name or settings.SIGNATURE_PROVIDER or "noop").lower())
        signature_request = await provider.send_for_signature(
            document=document,
            file_name=f"{letter.reference_number.replace('/', '_')}.pdf",
            signer_name=employee.full_name,
            signer_email=employee.work_email,
            reason=letter.category.value,
        )
    except SignatureError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    letter.signature_provider = signature_request.provider
    letter.signature_request_id = signature_request.request_id
    letter.status = LetterStatus.SENT

    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="issued_letter",
        entity_id=letter.id,
        actor=user,
        changes={
            "provider": signature_request.provider,
            "request_id": signature_request.request_id,
        },
        summary=f"Sent {letter.reference_number} for signature",
        request=request,
    )
    await session.commit()
    await session.refresh(letter)
    return (await _letter_reads(session, user, [letter]))[0]


async def record_signature_webhook(
    session: AsyncSession, *, provider_name: str, raw_body: bytes, headers: dict[str, str]
) -> dict[str, str]:
    """Verify, then apply, a provider's signature completion.

    Verification happens **before** the payload is parsed or trusted. The
    webhook is not tenant-scoped — the provider does not know about tenants —
    so the request id, which we minted, is the lookup key.
    """
    try:
        provider = build_provider(provider_name)
    except SignatureError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    if not provider.verify_webhook(raw_body=raw_body, headers=headers):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Webhook signature is not valid"
        )

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Webhook body is not valid JSON"
        ) from exc

    signature_status = provider.parse_webhook(payload)
    if signature_status.state != "signed":
        return {"status": "ignored", "state": signature_status.state}

    letter = await session.scalar(
        select(IssuedLetter).where(IssuedLetter.signature_request_id == signature_status.request_id)
    )
    if letter is None:
        # 200 rather than 404: providers retry on non-2xx, and retrying a
        # webhook for a letter we do not have will never succeed.
        return {"status": "unknown_request"}

    letter.status = LetterStatus.SIGNED
    letter.signed_at = signature_status.signed_at

    try:
        signed_document = await provider.download_signed(signature_status.request_id)
        storage = get_storage()
        key = storage.new_key(letter.tenant_id, "letters", ".pdf")
        await storage.put(key, signed_document)
        letter.signed_pdf_storage_key = key
    except (SignatureError, StorageError):
        # The status change stands even if the download fails; a retry can
        # fetch the document later.
        pass

    await audit.record(
        session,
        tenant_id=letter.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="issued_letter",
        entity_id=letter.id,
        actor=None,
        actor_type="system",
        changes={"provider": provider_name, "request_id": signature_status.request_id},
        summary=f"Letter {letter.reference_number} signed",
    )
    await session.commit()
    return {"status": "ok"}
