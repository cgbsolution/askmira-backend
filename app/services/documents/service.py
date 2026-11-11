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
from decimal import ROUND_HALF_UP, Decimal
from html import escape as html_escape
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import desc, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.config import settings
from app.core.deps import tenant_filter, tenant_scoped
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
from app.models.integrations import TenantConnection
from app.models.letter_acknowledgement import LetterAcknowledgement
from app.models.notifications import Channel, DigestMode
from app.models.payroll import EmployeeSalary
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.schemas.letters import (
    DraftPreviewRequest,
    LetterContentRead,
    LetterPdfPreviewRequest,
    LetterRead,
    MergeFieldsRead,
    PrefillRead,
    PreviewRead,
    PreviewRequest,
    SignatureProviderRead,
    TemplateFieldInput,
    TemplateUpdate,
    VerifyRead,
)
from app.services import audit
from app.services.connections import digio as digio_connection
from app.services.connections import store as connection_store
from app.services.documents import letterhead as letterhead_service
from app.services.documents import signed_copy
from app.services.documents.annexure import build_annexure
from app.services.documents.default_templates import DEFAULT_TEMPLATES, DEFAULTS_VERSION
from app.services.documents.letter_merge import (
    AVAILABLE_FIELDS,
    COMPUTED_HANDLERS,
    FIELD_LABELS,
    LetterMergeError,
    MergeContext,
    extract_tokens,
    long_date,
    period_in_words,
    preview_render,
    render_template,
    resolve_plain,
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

#: By email only: the email carries the link the letter is read and signed from,
#: and employees have no letters in the portal to open from a bell notice.
_PROMPT = {Channel.EMAIL: DigestMode.IMMEDIATE}
register_event(
    "documents.letter.issued",
    "Your {letter_name} is ready",
    "Your {letter_name} ({reference_number}) has been issued. Read it, sign it and accept it "
    "from the button in this email. You do not need to sign in.",
    preferences=_PROMPT,
)
register_event(
    "documents.letter.acknowledgement_requested",
    "Please acknowledge your {letter_name}",
    "{actor_name} asked you to read, sign and accept your {letter_name} "
    "({reference_number}). Open it from the button in this email. You do not need to "
    "sign in.",
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
    # Local: letter_link reads letters through this module.
    from app.services.documents import letter_link

    await notify(
        session,
        tenant_id=user.tenant_id,
        event_key="documents.letter.issued",
        recipient_user_ids=recipients,
        payload={
            "letter_name": letter_name,
            "reference_number": letter.reference_number,
            **letter_link.payload_for(letter),
        },
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


#: Employee fields worked out here or looked up by `_build_context`, not columns.
_EMPLOYEE_DERIVED = (
    "salutation",
    "full_name",
    "notice_period",
    "probation_period",
    "location",
    "location_city",
    "department",
    "company",
    "manager_name",
)


def _salutation(employee: Employee) -> str:
    gender = getattr(getattr(employee, "gender", None), "value", None)
    if gender == "male":
        return "Mr."
    if gender == "female":
        return "Ms."
    return "Mr. / Ms."


def _months_between(start: date, end: date) -> int | None:
    months = (end.year - start.year) * 12 + (end.month - start.month)
    if end.day + 1 < start.day:
        months -= 1
    return months if months > 0 else None


def _employee_values(employee: Employee) -> dict[str, Any]:
    """Exactly the keys `AVAILABLE_FIELDS["employee"]` promises.

    The derived ones that need another table (location, department, company,
    manager) start empty; `_build_context` fills them.
    """
    values = {
        key: getattr(employee, key)
        for key in AVAILABLE_FIELDS["employee"]
        if key not in _EMPLOYEE_DERIVED
    }
    values.update(dict.fromkeys(_EMPLOYEE_DERIVED))
    if values.get("first_name") is not None:
        values["salutation"] = _salutation(employee)
        values["full_name"] = " ".join(
            part for part in (employee.first_name, employee.last_name) if part
        )
    notice = values.get("notice_period_days")
    if isinstance(notice, int):
        values["notice_period"] = period_in_words(notice) or None
    joined, probation_end = values.get("date_of_joining"), values.get("probation_end_date")
    if isinstance(joined, date) and isinstance(probation_end, date):
        months = _months_between(joined, probation_end)
        if months:
            values["probation_period"] = period_in_words(months * 30)
    return values


#: Tokens that need the salary annexure worked out (a payroll computation).
_ANNEXURE_TOKENS = frozenset(
    {
        "computed.salary_annexure",
        "salary.gross_monthly",
        "salary.gross_annual",
        "salary.net_monthly",
        "salary.net_annual",
    }
)

_EFFECTIVE_LABELS = {
    LetterCategory.PROMOTION: "Appraisal W.E.F.",
    LetterCategory.OFFER: "Date of Joining",
    LetterCategory.APPOINTMENT: "Date of Joining",
}


async def _lookups(session: AsyncSession, user: User, employee: Employee) -> dict[str, Any]:
    """Location, department, company and manager: names, not ids."""
    from app.models.org import Department, LegalEntity, Location

    found: dict[str, Any] = {}

    async def one(model, row_id):
        if row_id is None:
            return None
        return await session.scalar(
            tenant_scoped(select(model).where(model.id == row_id), model, user)
        )

    location = await one(Location, employee.location_id)
    if location is not None:
        found["location"] = location.city or location.name
        found["location_city"] = location.city or location.name
    department = await one(Department, employee.department_id)
    if department is not None:
        found["department"] = department.name
    entity = await one(LegalEntity, employee.legal_entity_id)
    if entity is not None:
        found["company"] = entity.name
    manager = await one(Employee, employee.manager_id)
    if manager is not None:
        found["manager_name"] = " ".join(p for p in (manager.first_name, manager.last_name) if p)
    return found


async def _build_context(
    session: AsyncSession,
    user: User,
    employee: Employee,
    *,
    as_of: date,
    manual: dict,
    issued_on: date | None = None,
    letterhead: dict[str, Any] | None = None,
    tokens: set[str] | None = None,
    category: LetterCategory | None = None,
) -> MergeContext:
    """Assemble merge sources, resolved as of a date.

    `as_of` matters: a relieving letter issued today for an exit last month must
    show the salary that was in force at exit, not today's. `issued_on` is the
    day the letter is dated. `tokens`, when given, lets the salary annexure (a
    payroll computation) be skipped for a letter that does not show it.
    """
    tenant = await session.get(Tenant, user.tenant_id)
    salary = await active_salary_on(session, user, employee.id, as_of)
    if letterhead is None:
        letterhead = await letterhead_service.current_snapshot(session, user)

    salary_dict: dict = {}
    components: list[dict] = []
    if salary is not None:
        salary_dict = {
            # Whole rupees, as a letter states a CTC: a typed 6,20,000.04 is 6,20,000.
            "ctc_annual": Decimal(str(salary.ctc_annual)).quantize(
                Decimal(1), rounding=ROUND_HALF_UP
            ),
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
    employee_values = _employee_values(employee)
    employee_values.update(await _lookups(session, user, employee))
    profile, previous_jobs = await _profile_values(session, user, employee)

    annexure: dict[str, Any] | None = None
    if salary is not None:
        salary_dict["ctc_monthly"] = (salary_dict["ctc_annual"] / 12).quantize(Decimal("1"))
        if tokens is None or tokens & _ANNEXURE_TOKENS:
            annexure = await build_annexure(
                session,
                user,
                employee,
                salary,
                as_of=as_of,
                header={
                    "name": employee_values.get("full_name"),
                    "location": employee_values.get("location"),
                    "designation": employee.designation,
                    "effective_label": _EFFECTIVE_LABELS.get(category, "Effective from")
                    if category
                    else "Effective from",
                    "effective_from": (
                        employee.date_of_joining
                        if category in (LetterCategory.OFFER, LetterCategory.APPOINTMENT)
                        else salary.effective_from
                    ),
                },
            )
            if not annexure.get("error"):
                salary_dict["gross_monthly"] = annexure["gross"][0]
                salary_dict["gross_annual"] = annexure["gross"][1]
                salary_dict["net_monthly"] = annexure["net"][0]
                salary_dict["net_annual"] = annexure["net"][1]

    return MergeContext(
        as_of=as_of,
        employee=employee_values,
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
        company=letterhead_service.company_values(letterhead),
        profile=profile,
        previous_employment=previous_jobs,
        annexure=annexure,
        signature_html=await letterhead_service.signature_html(letterhead),
        issued_on=issued_on or date.today(),
    )


_STATE_NAMES = {
    "AN": "Andaman and Nicobar Islands", "AP": "Andhra Pradesh", "AR": "Arunachal Pradesh",
    "AS": "Assam", "BR": "Bihar", "CH": "Chandigarh", "CG": "Chhattisgarh", "CT": "Chhattisgarh",
    "DN": "Dadra and Nagar Haveli and Daman and Diu", "DD": "Daman and Diu", "DL": "Delhi",
    "GA": "Goa", "GJ": "Gujarat", "HR": "Haryana", "HP": "Himachal Pradesh",
    "JK": "Jammu and Kashmir", "JH": "Jharkhand", "KA": "Karnataka", "KL": "Kerala",
    "LA": "Ladakh", "LD": "Lakshadweep", "MP": "Madhya Pradesh", "MH": "Maharashtra",
    "MN": "Manipur", "ML": "Meghalaya", "MZ": "Mizoram", "NL": "Nagaland", "OR": "Odisha",
    "OD": "Odisha", "PY": "Puducherry", "PB": "Punjab", "RJ": "Rajasthan", "SK": "Sikkim",
    "TN": "Tamil Nadu", "TG": "Telangana", "TS": "Telangana", "TR": "Tripura",
    "UP": "Uttar Pradesh", "UK": "Uttarakhand", "UT": "Uttarakhand", "WB": "West Bengal",
}  # fmt: skip


def _street(address: dict | None) -> str | None:
    if not address:
        return None
    parts = [address.get("line1"), address.get("line2"), address.get("district")]
    return ", ".join(str(p) for p in parts if p) or None


async def _profile_values(
    session: AsyncSession, user: User, employee: Employee
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Exactly `AVAILABLE_FIELDS["profile"]`, plus previous employers."""
    from app.core.crypto import decrypt_secret
    from app.models.disbursement import EmployeeBankAccount
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
    current = addresses.get("current") or {}
    permanent = addresses.get("permanent") or (
        current if addresses.get("permanent_same_as_current") else {}
    )
    family = (profile.family_members if profile else None) or []
    mother = next((m for m in family if str(m.get("relationship", "")).lower() == "mother"), None)
    contacts = (profile.emergency_contacts if profile else None) or []
    contact = next((c for c in contacts if c.get("is_primary")), contacts[0] if contacts else None)
    education = (profile.education if profile else None) or []
    highest = next((e for e in education if e.get("is_highest")), None) or (
        max(education, key=lambda e: e.get("end_year") or 0) if education else None
    )
    jobs = list((profile.previous_employment if profile else None) or [])

    months = 0
    for job in jobs:
        with contextlib.suppress(TypeError, ValueError):
            start = date.fromisoformat(str(job.get("from_date")))
            end = date.fromisoformat(str(job.get("to_date")))
            months += max(0, (end.year - start.year) * 12 + end.month - start.month)
    experience = ""
    if months:
        years, rest = divmod(months, 12)
        bits = [
            f"{years} year{'s' if years != 1 else ''}" if years else "",
            f"{rest} month{'s' if rest != 1 else ''}" if rest else "",
        ]
        experience = " ".join(b for b in bits if b)

    bank = await session.scalar(
        tenant_scoped(
            select(EmployeeBankAccount)
            .where(EmployeeBankAccount.employee_id == employee.id)
            .order_by(desc(EmployeeBankAccount.created_at)),
            EmployeeBankAccount,
            user,
        ).limit(1)
    )

    def state(address: dict) -> str | None:
        code = address.get("state_code")
        return _STATE_NAMES.get(str(code).upper(), code) if code else None

    relation = None
    if contact:
        relation = (
            contact.get("relationship_text")
            or str(contact.get("relationship") or "").replace("_", " ").capitalize()
            or None
        )

    values = {
        "father_name": personal.get("father_name"),
        "mother_name": mother.get("name") if mother else None,
        "spouse_name": personal.get("spouse_name"),
        "blood_group": personal.get("blood_group"),
        "marital_status": str(personal.get("marital_status") or "").replace("_", " ").capitalize()
        or None,
        "nationality": personal.get("nationality"),
        "personal_email": employee.personal_email,
        "permanent_address": _street(permanent),
        "permanent_city": permanent.get("city"),
        "permanent_state": state(permanent),
        "permanent_pincode": permanent.get("pincode"),
        "current_address": _street(current),
        "current_city": current.get("city"),
        "current_state": state(current),
        "current_pincode": current.get("pincode"),
        "emergency_name": contact.get("name") if contact else None,
        "emergency_relation": relation,
        "emergency_phone": contact.get("phone") if contact else None,
        "highest_education": (
            highest.get("qualification") or highest.get("course") if highest else None
        ),
        "specialisation": (
            highest.get("specialisation") or highest.get("stream") if highest else None
        ),
        "total_experience": experience or None,
        "bank_name": bank.bank_name if bank else None,
        "account_holder_name": bank.account_holder_name if bank else None,
        "account_number": (decrypt_secret(bank.account_number, user.tenant_id) if bank else None)
        or employee.bank_account_number,
        "ifsc": (bank.ifsc if bank else None) or employee.bank_ifsc,
    }

    def parsed(job: dict) -> dict:
        out = dict(job)
        for key in ("from_date", "to_date"):
            with contextlib.suppress(TypeError, ValueError):
                out[key] = date.fromisoformat(str(job.get(key)))
        return out

    return values, [parsed(job) for job in jobs]


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


def _sample_context(
    tenant: Tenant | None,
    *,
    as_of: date,
    manual: dict,
    letterhead: dict[str, Any] | None = None,
    signature: str = "",
) -> MergeContext:
    """Placeholders for a preview with nobody chosen.

    Every employee value is its own label in brackets, so nothing in the preview
    can be mistaken for a real person's data. There is no salary: salary tokens
    show as holes until a real employee is picked.
    """
    employee = {key: f"[{key.replace('_', ' ')}]" for key in AVAILABLE_FIELDS["employee"]}
    for key in ("date_of_joining", "date_of_exit", "probation_end_date", "notice_period_days"):
        employee[key] = None
    statutory = {key: f"[{key.replace('_', ' ')}]" for key in AVAILABLE_FIELDS["statutory"]}
    profile = {key: f"[{key.replace('_', ' ')}]" for key in AVAILABLE_FIELDS["profile"]}
    return MergeContext(
        as_of=as_of,
        employee=employee,
        salary={},
        tenant={"name": tenant.name if tenant else "", "slug": tenant.slug if tenant else ""},
        manual=manual,
        statutory=statutory,
        company=letterhead_service.company_values(letterhead or {}),
        profile=profile,
        signature_html=signature,
        issued_on=date.today(),
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


async def ensure_default_templates(session: AsyncSession, user: User) -> int:
    """Install the default letters (offer, promotion, experience, joining form)
    once per workspace. Returns how many were added.

    Recorded in `tenant.settings["letter_defaults"]`, so a default HR later
    switched off or replaced is not put back. A code already in use is skipped.
    """
    tenant = await session.get(Tenant, user.tenant_id)
    if tenant is None:
        return 0
    installed = int((tenant.settings or {}).get("letter_defaults") or 0)
    if installed >= DEFAULTS_VERSION:
        return 0

    added: list[str] = []
    for spec in DEFAULT_TEMPLATES:
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
            body_html=spec["body_html"].strip(),
            version=1,
            active=True,
            is_seed=False,
            use_letterhead=True,
        )
        for item in spec["fields"]:
            template.fields.append(LetterTemplateField(tenant_id=user.tenant_id, **item))
        session.add(template)
        added.append(spec["code"])

    stored = dict(tenant.settings or {})
    stored["letter_defaults"] = DEFAULTS_VERSION
    tenant.settings = stored
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="letter_template",
        actor=user,
        changes={"installed": added, "defaults_version": DEFAULTS_VERSION},
        summary=f"Installed {len(added)} default letter template(s)",
    )
    await session.commit()
    return len(added)


async def list_templates(
    session: AsyncSession, user: User, *, include_inactive: bool = False
) -> list[LetterTemplate]:
    """Active versions for everybody; every version for HR when asked.

    HR's first look installs the default letters (see `ensure_default_templates`).
    """
    if permissions.is_hr(user):
        await ensure_default_templates(session, user)
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


#: The token prefixes a declared field of each source may use. The statutory
#: and profile values are the employee's; the letterhead's company details
#: are the workspace's.
_SOURCE_PREFIX = {
    FieldSource.EMPLOYEE: ("employee", "statutory", "profile"),
    FieldSource.SALARY: ("salary",),
    FieldSource.TENANT: ("tenant", "company"),
    FieldSource.MANUAL: ("manual",),
    FieldSource.COMPUTED: ("computed",),
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
        if field.token.partition(".")[0] not in _SOURCE_PREFIX[field.source]:
            problems.append(
                f"'{field.token}' is declared as a {field.source.value} field but its token "
                f"starts with '{field.token.partition('.')[0]}'"
            )
        problems.extend(template_token_problems("{{" + field.token + "}}"))
        if field.source_path:
            if field.source is not FieldSource.MANUAL:
                problems.append(
                    f"'{field.token}': only a manual field can be pre-filled from another field"
                )
            elif field.source_path.startswith(("manual.", "computed.")):
                problems.append(
                    f"'{field.token}' is pre-filled from '{field.source_path}', which is not a "
                    "record field. Use an employee, salary, company or profile field."
                )
            else:
                problems.extend(template_token_problems("{{" + field.source_path + "}}"))
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
            footer_html=f'<p style="font-size:8pt;color:#7a6e68">{REVIEW_BANNER}</p>',
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
            "required manual field so issuance prompts for it, and give it a field to "
            "start from (say employee.notice_period) to have it filled in for you."
        ),
        labels=FIELD_LABELS,
    )


# ── Preview ─────────────────────────────────────────────────────────────────


def _fill_defaults(context: MergeContext, fields: list[Any]) -> dict[str, str]:
    """Pre-fill each manual field left blank from the field it starts from.

    "Notice period" on a promotion letter starts as the employee's notice
    period in words; HR may type something else, and what they type wins.
    """
    filled: dict[str, str] = {}
    for field in fields:
        path = getattr(field, "source_path", None)
        if field.source != FieldSource.MANUAL or not path:
            continue
        key = field.token.split(".")[-1]
        if str(context.manual.get(key) or "").strip():
            continue
        try:
            value = resolve_plain(path, context)
        except LetterMergeError:
            continue
        if value:
            context.manual[key] = value
            filled[key] = value
    # A promotion letter's annexure names the new designation, as typed.
    designation = str(context.manual.get("designation") or "").strip()
    if designation and context.annexure and not context.annexure.get("error"):
        context.annexure["designation"] = designation
    return filled


async def default_as_of(
    session: AsyncSession, user: User, employee: Employee, category: LetterCategory | None
) -> date:
    """The date a letter's values resolve on when HR does not choose one.

    A promotion letter is written before the raise starts and must carry the
    new salary, so it reads the latest revision when that is still ahead. An
    offer or appointment letter reads the joining date; a relieving or
    experience letter the last working day.
    """
    today = date.today()
    if category is LetterCategory.PROMOTION:
        latest = await session.scalar(
            tenant_scoped(
                select(EmployeeSalary.effective_from)
                .where(EmployeeSalary.employee_id == employee.id)
                .order_by(desc(EmployeeSalary.effective_from)),
                EmployeeSalary,
                user,
            ).limit(1)
        )
        if latest is not None and latest > today:
            return latest
    if category in (LetterCategory.OFFER, LetterCategory.APPOINTMENT):
        joined = employee.date_of_joining
        if joined is not None and joined > today:
            return joined
    if category in (LetterCategory.RELIEVING, LetterCategory.EXPERIENCE):
        left = employee.date_of_exit
        if left is not None and left < today:
            return left
    return today


async def _preview(
    session: AsyncSession,
    user: User,
    *,
    body_html: str,
    header_html: str | None,
    footer_html: str | None,
    fields: list[Any],
    request: PreviewRequest,
    category: LetterCategory | None = None,
) -> PreviewRead:
    manual = dict(request.manual_values)
    letterhead = await letterhead_service.current_snapshot(session, user)
    tokens = extract_tokens(header_html, body_html, footer_html)

    if request.employee_id is not None:
        employee = await _employee(session, user, request.employee_id)
        as_of = request.as_of or await default_as_of(session, user, employee, category)
        context = await _build_context(
            session,
            user,
            employee,
            as_of=as_of,
            manual=manual,
            letterhead=letterhead,
            tokens=tokens,
            category=category,
        )
        _fill_defaults(context, fields)
        label: str | None = f"{employee.full_name} ({employee.employee_code})"
    else:
        as_of = request.as_of or date.today()
        tenant = await session.get(Tenant, user.tenant_id)
        context = _sample_context(
            tenant,
            as_of=as_of,
            manual=manual,
            letterhead=letterhead,
            signature=await letterhead_service.signature_html(letterhead),
        )
        _fill_defaults(context, fields)
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
            if not context.manual.get(key):
                # One plain line instead of the merge engine's "could not be resolved".
                problems = [p for p in problems if not p.startswith(f"{field.token}:")]
                problems.append(f"'{field.label}' is typed in when the letter is issued")

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
        category=template.category,
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
        category=request.category,
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
    effective_as_of = as_of or await default_as_of(session, user, employee, template.category)
    manual = {k: v for k, v in (manual_values or {}).items() if str(v).strip()}
    letterhead = await letterhead_service.current_snapshot(session, user)
    if not template.use_letterhead:
        letterhead = {**letterhead, "design": "none"}

    context = await _build_context(
        session,
        user,
        employee,
        as_of=effective_as_of,
        manual=manual,
        letterhead=letterhead,
        tokens=extract_tokens(template.header_html, template.body_html, template.footer_html),
        category=template.category,
    )
    _fill_defaults(context, list(template.fields))

    # Required MANUAL fields the template declares must be supplied before we
    # bother merging — a clearer error than "token resolved to nothing".
    missing = [
        f.label
        for f in template.fields
        if f.required
        and f.source.value == "manual"
        and not context.manual.get(f.token.split(".")[-1])
    ]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"These fields are required for this letter: {', '.join(missing)}",
        )
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
        detail = str(exc)
        if exc.empty_tokens:
            # Say it in words: "Last working day is empty on Deepak's record."
            labels = {f.token: f.label for f in template.fields}
            names = [labels.get(t) or FIELD_LABELS.get(t) or t for t in exc.empty_tokens]
            person = " ".join(p for p in (employee.first_name, employee.last_name) if p)
            detail = (
                f"This letter needs {', '.join(names)}, which "
                f"{'is' if len(names) == 1 else 'are'} empty on {person}'s record. "
                "Fill it in on their profile, then issue the letter again."
            )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail
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
        letterhead=letterhead,
        manual_values=_serialise(context.manual),
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
            pdf_bytes = await render_letter_pdf(merged, reference, letterhead, tenant)
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


async def render_letter_pdf(
    merged_html: str, reference_number: str, letterhead: dict | None, tenant: Tenant | None
) -> bytes:
    """A letter as a PDF, on the letterhead it was issued with.

    A letter issued before letterheads existed has none recorded and keeps
    rendering on the plain header it went out with.
    """
    if letterhead is None:
        return render_pdf(
            merged_html=merged_html,
            reference_number=reference_number,
            letterhead=Letterhead(company_name=tenant.name if tenant else "AskMiraa"),
        )
    return render_pdf(
        merged_html=merged_html,
        reference_number=reference_number,
        art=await letterhead_service.art_for(letterhead),
        show_reference=bool(letterhead.get("show_reference", True)),
        highlight_colour=str(letterhead.get("highlight_colour") or "#E00000"),
    )


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
        data = await render_letter_pdf(
            letter.merged_html, letter.reference_number, letter.letterhead, tenant
        )
    except PdfRenderError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if letter.pdf_storage_key:
        with contextlib.suppress(StorageError):
            await get_storage().put(letter.pdf_storage_key, data)
    return data


async def pdf_bytes(session: AsyncSession, letter: IssuedLetter) -> bytes:
    """The letter's PDF, for a caller that has already checked its content hash."""
    return await _pdf_bytes(session, letter)


async def _signed_copy_key(session: AsyncSession, letter: IssuedLetter) -> str | None:
    """Where the copy printed with the person's signature when they accepted it is."""
    return await session.scalar(
        select(LetterAcknowledgement.signed_copy_storage_key).where(
            tenant_filter(LetterAcknowledgement, letter.tenant_id),
            LetterAcknowledgement.letter_id == letter.id,
        )
    )


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


async def prefill(
    session: AsyncSession,
    user: User,
    template_id: uuid.UUID,
    employee_id: uuid.UUID,
    as_of: date | None = None,
) -> PrefillRead:
    """What the issue form starts with for one person.

    Each manual field that starts from a record field gets that field's value,
    so HR sees "two months" already in "Notice period" and may change it.
    """
    template = await get_template(session, user, template_id)
    employee = await _employee(session, user, employee_id)
    effective = as_of or await default_as_of(session, user, employee, template.category)
    context = await _build_context(
        session,
        user,
        employee,
        as_of=effective,
        manual={},
        tokens={f.source_path for f in template.fields if f.source_path},
        category=template.category,
    )
    return PrefillRead(values=_fill_defaults(context, list(template.fields)), as_of=effective)


async def pdf_preview(session: AsyncSession, user: User, request: LetterPdfPreviewRequest) -> bytes:
    """A letter as it would print, for a saved template or unsaved content.

    Nothing is issued or numbered: the reference line reads PREVIEW, and a
    value that is missing shows as a marked hole instead of blocking.
    """
    if request.template_id is not None:
        template = await get_template(session, user, request.template_id)
        body, header, footer = template.body_html, template.header_html, template.footer_html
        fields: list[Any] = list(template.fields)
        use_letterhead, category = template.use_letterhead, template.category
    else:
        if not (request.body_html or "").strip():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Write the letter before previewing it.",
            )
        body, header, footer = request.body_html or "", request.header_html, request.footer_html
        fields, use_letterhead, category = request.fields, request.use_letterhead, request.category

    rendered = await _preview(
        session,
        user,
        body_html=body,
        header_html=header,
        footer_html=footer,
        fields=fields,
        request=request,
        category=category,
    )
    letterhead = await letterhead_service.current_snapshot(session, user)
    if not use_letterhead:
        letterhead = {**letterhead, "design": "none"}
    tenant = await session.get(Tenant, user.tenant_id)
    try:
        return await render_letter_pdf(rendered.html, "PREVIEW", letterhead, tenant)
    except PdfRenderError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


async def letterhead_preview(session: AsyncSession, user: User, settings_in: Any) -> bytes:
    """A sample letter on unsaved letterhead choices, for the settings screen."""
    current = await letterhead_service.current_snapshot(session, user)
    tenant = await session.get(Tenant, user.tenant_id)
    chosen = {**current, **settings_in.model_dump(mode="json")}
    chosen["company_name"] = chosen.get("company_name") or (tenant.name if tenant else "")
    if not chosen.get("show_logo"):
        chosen["logo_key"] = None
    sample = letterhead_service.SAMPLE_LETTER.format(
        company=html_escape(chosen["company_name"]),
        title=html_escape(chosen.get("signatory_title") or ""),
        signature=await letterhead_service.signature_html(chosen),
        date=long_date(date.today()),
    )
    try:
        return await render_letter_pdf(sample, "PREVIEW", chosen, tenant)
    except PdfRenderError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


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
    # An employee reads, signs and downloads their letters only from the link in
    # the letter's email (letter_link), never in the portal.
    if not is_hr:
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
            has_signed_pdf=bool(
                letter.signed_pdf_storage_key
                or (acks.get(letter.id) and acks[letter.id].signed_copy_storage_key)
            ),
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
        # Employees see no letters in the portal: each one reaches them as a
        # link in its email, where they read, sign and download it.
        return []
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
    pages = 0
    signed_key = await _signed_copy_key(session, letter)
    if intact or signed_key:
        with contextlib.suppress(HTTPException):
            pages = signed_copy.page_count(await _shown_document(session, letter, intact))
    return LetterContentRead(
        reference_number=letter.reference_number,
        merged_html=letter.merged_html,
        intact=intact,
        page_count=pages,
        showing_signed_copy=bool(signed_key),
    )


async def _shown_document(session: AsyncSession, letter: IssuedLetter, intact: bool) -> bytes:
    """The PDF the viewer shows: the signed copy once accepted, else the letter."""
    data = await stored_pdf(await _signed_copy_key(session, letter) or "")
    if data is not None:
        return data
    if not intact:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Letter {letter.reference_number} does not match its stored content hash.",
        )
    return await _pdf_bytes(session, letter)


async def letter_page(
    session: AsyncSession, user: User, letter_id: uuid.UUID, number: int
) -> bytes:
    """One page of the letter as a picture, exactly as it prints."""
    letter, intact = await verify_letter(session, user, letter_id)
    _ensure_reader(letter, is_hr=permissions.is_hr(user), own_employee_id=user.employee_id)
    return signed_copy.page_image(await _shown_document(session, letter, intact), number)


async def letter_verify(session: AsyncSession, user: User, letter_id: uuid.UUID) -> VerifyRead:
    letter, intact = await verify_letter(session, user, letter_id)
    _ensure_reader(letter, is_hr=permissions.is_hr(user), own_employee_id=user.employee_id)
    return VerifyRead(
        reference_number=letter.reference_number, content_hash=letter.content_hash, intact=intact
    )


async def download_stem(session: AsyncSession, letter: IssuedLetter) -> str:
    return await _download_stem(session, letter)


async def _download_stem(session: AsyncSession, letter: IssuedLetter) -> str:
    """ "Promotion Letter - Deepak Rawat - CGB_PRO_2026-27_00001": what it is, whose, which."""
    import re

    template = await session.get(LetterTemplate, letter.template_id)
    person = None
    if letter.employee_id is not None:
        employee = await session.get(Employee, letter.employee_id)
        if employee is not None:
            person = " ".join(p for p in (employee.first_name, employee.last_name) if p)
    parts = [template.name if template else None, person, letter.reference_number]
    stem = " - ".join(p for p in parts if p).replace("/", "_")
    return re.sub(r"[^A-Za-z0-9 ._()-]+", "", stem).strip() or "letter"


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
    stem = await _download_stem(session, letter)
    if signed:
        # A provider's signed PDF first; else the copy printed when it was accepted.
        data = await stored_pdf(letter.signed_pdf_storage_key or "")
        if data is None:
            data = await stored_pdf(await _signed_copy_key(session, letter) or "")
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


async def provider_for(
    session: AsyncSession, tenant_id: uuid.UUID, provider_name: str | None = None
):
    """The provider a workspace's letters go out on.

    The workspace's own Digio account (Settings → Integrations) first; otherwise
    the deployment's configured provider.
    """
    name = (provider_name or "").lower()
    if name in ("", "digio"):
        own = await digio_connection.provider_for_tenant(session, tenant_id)
        if own is not None:
            return own
    return build_provider(name or (settings.SIGNATURE_PROVIDER or "noop").lower())


async def signature_provider_status(session: AsyncSession, user: User) -> SignatureProviderRead:
    """Whether letters can really be e-signed for this workspace. Said plainly."""
    own = await connection_store.get(session, user, connection_store.KIND_DIGIO)
    if own is not None and own.enabled and own.secret_encrypted:
        environment = (own.settings or {}).get("environment", "production")
        return SignatureProviderRead(
            provider="digio",
            connected=True,
            detail=(
                f"Your own Digio account ({environment}) is connected. Letters go out for "
                "Aadhaar eSign and Digio emails each signer the link."
            ),
        )
    return _deployment_provider_status()


def _deployment_provider_status() -> SignatureProviderRead:
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
        provider = await provider_for(session, user.tenant_id, provider_name)
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
    """Verify, then apply, a provider's signature completion (deployment account).

    Verification happens **before** the payload is parsed or trusted. The
    webhook is not tenant-scoped — the provider does not know about tenants —
    so the request id, which we minted, is the lookup key.
    """
    try:
        provider = build_provider(provider_name)
    except SignatureError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return await _apply_webhook(
        session, provider, provider_name, raw_body=raw_body, headers=headers, tenant_id=None
    )


async def record_tenant_signature_webhook(
    session: AsyncSession, *, connection_id: uuid.UUID, raw_body: bytes, headers: dict[str, str]
) -> dict[str, str]:
    """A signature completion for one workspace's own Digio account.

    The address names the workspace's connection; the workspace's own webhook
    secret proves the call came from Digio. Only that workspace's letters can
    be touched.
    """
    row = await session.get(TenantConnection, connection_id)
    if row is None or row.kind != connection_store.KIND_DIGIO or not row.enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown webhook.")
    provider = await digio_connection.provider_for_tenant(session, row.tenant_id)
    if provider is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown webhook.")
    return await _apply_webhook(
        session, provider, "digio", raw_body=raw_body, headers=headers, tenant_id=row.tenant_id
    )


async def _apply_webhook(
    session: AsyncSession,
    provider,
    provider_name: str,
    *,
    raw_body: bytes,
    headers: dict[str, str],
    tenant_id: uuid.UUID | None,
) -> dict[str, str]:
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

    stmt = select(IssuedLetter).where(
        IssuedLetter.signature_request_id == signature_status.request_id
    )
    if tenant_id is not None:
        stmt = stmt.where(tenant_filter(IssuedLetter, tenant_id))
    letter = await session.scalar(stmt)
    if letter is None:
        # 200 rather than 404: providers retry on non-2xx, and retrying a
        # webhook for a letter we do not have will never succeed.
        return {"status": "unknown_request"}
    if letter.status is LetterStatus.SIGNED:
        return {"status": "ok"}

    await _mark_signed(session, letter, provider, provider_name, signature_status)
    await session.commit()
    return {"status": "ok"}


async def _mark_signed(
    session: AsyncSession, letter: IssuedLetter, provider, provider_name: str, signature_status
) -> None:
    """Record a signature: status, the signed PDF when it can be fetched, the audit row."""
    letter.status = LetterStatus.SIGNED
    letter.signed_at = signature_status.signed_at or datetime.now(UTC)

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


async def refresh_signature_status(
    session: AsyncSession, user: User, letter_id: uuid.UUID
) -> LetterRead:
    """Ask the provider where a letter sent for signature stands, now.

    Works without a webhook: the letter moves to Signed (and the signed PDF is
    kept) as soon as the provider says so.
    """
    letter = await _letter_for_hr(session, user, letter_id)
    if letter.status is not LetterStatus.SENT or not letter.signature_request_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only a letter that is out for signature can be checked.",
        )
    try:
        provider = await provider_for(session, user.tenant_id, letter.signature_provider)
        signature_status = await provider.get_status(letter.signature_request_id)
    except SignatureError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    if signature_status.state == "signed":
        await _mark_signed(
            session, letter, provider, letter.signature_provider or "", signature_status
        )
        await session.commit()
        await session.refresh(letter)
    return (await _letter_reads(session, user, [letter]))[0]


async def check_pending_signatures(session: AsyncSession, tenant_id: uuid.UUID) -> int:
    """Hourly: letters out for signature on the workspace's own Digio account. Signed count."""
    provider = await digio_connection.provider_for_tenant(session, tenant_id)
    if provider is None:
        return 0
    letters = (
        await session.scalars(
            select(IssuedLetter)
            .where(
                tenant_filter(IssuedLetter, tenant_id),
                IssuedLetter.status == LetterStatus.SENT,
                IssuedLetter.signature_provider == "digio",
                IssuedLetter.signature_request_id.is_not(None),
            )
            .limit(200)
        )
    ).all()
    signed = 0
    for letter in letters:
        try:
            signature_status = await provider.get_status(letter.signature_request_id)
        except SignatureError:
            continue
        if signature_status.state == "signed":
            await _mark_signed(session, letter, provider, "digio", signature_status)
            signed += 1
    if signed:
        await session.commit()
    return signed
