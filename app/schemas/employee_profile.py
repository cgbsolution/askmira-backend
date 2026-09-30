"""Extended employee profile contracts (FS-CHR-01/02): the sections, and change requests.

Every section is a typed model. A write names its section and carries that
section's model (`SectionWrite` is a discriminated union on `section`), so a
PUT of "education" cannot smuggle a PAN in with it, and an unknown key is a
422 rather than a silently stored field.

Which sections an employee edits directly and which go to HR is decided in
`app.services.employee_profile`, not here; the contracts are the same either
way, which is what lets an approved change request apply exactly what was
proposed.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from app.models.employee import ProfileChangeStatus
from app.schemas.org import INDIAN_STATE_CODES, PINCODE
from app.services import employee_identifiers as ids

PHONE = r"^[0-9+\-() ]{6,32}$"


class ProfileSection(str, enum.Enum):
    IDENTITY = "identity"
    PERSONAL = "personal"
    ADDRESSES = "addresses"
    EMERGENCY_CONTACTS = "emergency_contacts"
    FAMILY_MEMBERS = "family_members"
    NOMINEES = "nominees"
    EDUCATION = "education"
    PREVIOUS_EMPLOYMENT = "previous_employment"
    STATUTORY = "statutory"
    BANK_ACCOUNT = "bank_account"


SECTION_LABELS: dict[ProfileSection, str] = {
    ProfileSection.IDENTITY: "legal name and date of birth",
    ProfileSection.PERSONAL: "personal details",
    ProfileSection.ADDRESSES: "addresses",
    ProfileSection.EMERGENCY_CONTACTS: "emergency contacts",
    ProfileSection.FAMILY_MEMBERS: "family members",
    ProfileSection.NOMINEES: "nominees",
    ProfileSection.EDUCATION: "education",
    ProfileSection.PREVIOUS_EMPLOYMENT: "previous employment",
    ProfileSection.STATUTORY: "statutory identifiers",
    ProfileSection.BANK_ACCOUNT: "salary bank account",
}

#: What an employee changes on their own record straight away. Nothing here
#: reaches a statutory return or decides where money goes.
DIRECT_SECTIONS: frozenset[ProfileSection] = frozenset(
    {
        ProfileSection.PERSONAL,
        ProfileSection.EMERGENCY_CONTACTS,
        ProfileSection.FAMILY_MEMBERS,
        ProfileSection.EDUCATION,
        ProfileSection.PREVIOUS_EMPLOYMENT,
    }
)
#: What goes to HR as a change request: legal identity, statutory numbers, the
#: salary account, addresses (PT state, Form 16, PF KYC) and nominees.
SENSITIVE_SECTIONS: frozenset[ProfileSection] = frozenset(ProfileSection) - DIRECT_SECTIONS


def _strip(value: object) -> object:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# ── Identity (legal name, date of birth) ───────────────────────────────────


class IdentityDetails(_Section):
    first_name: str = Field(min_length=1, max_length=128)
    last_name: str | None = Field(default=None, max_length=128)
    date_of_birth: date | None = None

    _blank = field_validator("last_name", mode="before")(_strip)


# ── Personal ───────────────────────────────────────────────────────────────


class MaritalStatus(str, enum.Enum):
    SINGLE = "single"
    MARRIED = "married"
    SEPARATED = "separated"
    DIVORCED = "divorced"
    WIDOWED = "widowed"
    UNDISCLOSED = "undisclosed"


BloodGroup = Literal["A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-"]


class PersonalDetails(_Section):
    #: EPF Form 11 and the ECR ask for the father's or spouse's name.
    father_name: str | None = Field(default=None, max_length=255)
    spouse_name: str | None = Field(default=None, max_length=255)
    marital_status: MaritalStatus | None = None
    blood_group: BloodGroup | None = None
    nationality: str | None = Field(default=None, max_length=64)
    alternate_phone: str | None = Field(default=None, pattern=PHONE)

    _blank = field_validator(
        "father_name", "spouse_name", "nationality", "alternate_phone", mode="before"
    )(_strip)


# ── Addresses ──────────────────────────────────────────────────────────────


class Address(_Section):
    line1: str = Field(min_length=1, max_length=255)
    line2: str | None = Field(default=None, max_length=255)
    city: str = Field(min_length=1, max_length=128)
    district: str | None = Field(default=None, max_length=128)
    #: The state decides Professional Tax and appears on Form 16 and PF KYC, so
    #: it is a code the payroll engine knows, not free text.
    state_code: str = Field(min_length=2, max_length=2)
    pincode: str = Field(pattern=PINCODE)
    country_code: Literal["IN"] = "IN"

    _blank = field_validator("line2", "district", mode="before")(_strip)

    @field_validator("state_code", mode="before")
    @classmethod
    def _state(cls, value: object) -> object:
        code = str(value or "").strip().upper()
        if code not in INDIAN_STATE_CODES:
            raise ValueError(f"'{value}' is not an Indian state or union territory code")
        return code


class Addresses(_Section):
    current: Address | None = None
    permanent: Address | None = None
    permanent_same_as_current: bool = False

    @model_validator(mode="after")
    def _same(self) -> Addresses:
        if self.permanent_same_as_current:
            if self.current is None:
                raise ValueError("Enter the current address before marking it as permanent too.")
            self.permanent = self.current.model_copy()
        return self


# ── People around the employee ─────────────────────────────────────────────


class Relationship(str, enum.Enum):
    SPOUSE = "spouse"
    CHILD = "child"
    FATHER = "father"
    MOTHER = "mother"
    SIBLING = "sibling"
    FATHER_IN_LAW = "father_in_law"
    MOTHER_IN_LAW = "mother_in_law"
    GRANDPARENT = "grandparent"
    FRIEND = "friend"
    OTHER = "other"


class EmergencyContact(_Section):
    name: str = Field(min_length=1, max_length=255)
    relationship: Relationship
    phone: str = Field(pattern=PHONE)
    alternate_phone: str | None = Field(default=None, pattern=PHONE)
    email: EmailStr | None = None
    is_primary: bool = False

    _blank = field_validator("alternate_phone", "email", mode="before")(_strip)


class FamilyMember(_Section):
    name: str = Field(min_length=1, max_length=255)
    relationship: Relationship
    date_of_birth: date | None = None
    gender: Literal["female", "male", "non_binary", "undisclosed"] | None = None
    occupation: str | None = Field(default=None, max_length=128)
    #: Dependants are who insurance and some reimbursements cover.
    is_dependent: bool = False

    _blank = field_validator("occupation", mode="before")(_strip)


class NomineePurpose(str, enum.Enum):
    PF = "pf"
    EPS = "eps"
    GRATUITY = "gratuity"
    INSURANCE = "insurance"


class Nominee(_Section):
    """One nominee for one purpose. The same person nominated for PF and
    gratuity is two rows, because the shares are separate declarations."""

    name: str = Field(min_length=1, max_length=255)
    relationship: Relationship
    date_of_birth: date | None = None
    address: str | None = Field(default=None, max_length=512)
    purpose: NomineePurpose
    share_percent: Decimal = Field(gt=0, le=100, max_digits=5, decimal_places=2)
    #: Required for a minor nominee (checked against the workspace's today in
    #: the service, not the server's clock).
    guardian_name: str | None = Field(default=None, max_length=255)

    _blank = field_validator("address", "guardian_name", mode="before")(_strip)


# ── Education and previous employment ──────────────────────────────────────


class EducationLevel(str, enum.Enum):
    SECONDARY = "secondary"
    HIGHER_SECONDARY = "higher_secondary"
    DIPLOMA = "diploma"
    GRADUATE = "graduate"
    POSTGRADUATE = "postgraduate"
    DOCTORATE = "doctorate"
    CERTIFICATION = "certification"
    OTHER = "other"


class Education(_Section):
    level: EducationLevel
    qualification: str = Field(min_length=1, max_length=255)
    specialisation: str | None = Field(default=None, max_length=255)
    institution: str = Field(min_length=1, max_length=255)
    board_or_university: str | None = Field(default=None, max_length=255)
    start_year: int | None = Field(default=None, ge=1950, le=2100)
    end_year: int | None = Field(default=None, ge=1950, le=2100)
    grade: str | None = Field(default=None, max_length=32)

    _blank = field_validator(
        "specialisation", "board_or_university", "grade", mode="before"
    )(_strip)

    @model_validator(mode="after")
    def _years(self) -> Education:
        if self.start_year and self.end_year and self.end_year < self.start_year:
            raise ValueError("The course cannot end before it starts.")
        return self


class PreviousEmployment(_Section):
    employer: str = Field(min_length=1, max_length=255)
    designation: str | None = Field(default=None, max_length=255)
    from_date: date
    to_date: date
    location: str | None = Field(default=None, max_length=128)
    reason_for_leaving: str | None = Field(default=None, max_length=500)

    _blank = field_validator("designation", "location", "reason_for_leaving", mode="before")(
        _strip
    )

    @model_validator(mode="after")
    def _dates(self) -> PreviousEmployment:
        if self.to_date < self.from_date:
            raise ValueError(f"At {self.employer}, the end date is before the start date.")
        return self


# ── Statutory identifiers and bank ─────────────────────────────────────────


def _identifier(check):
    def run(value: object) -> object:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        try:
            return check(str(value))
        except ids.IdentifierError as exc:
            raise ValueError(str(exc)) from exc

    return run


class StatutoryDetails(_Section):
    """A write of statutory identifiers. Only the fields named are changed.

    `aadhaar` is the full twelve digits, validated with its check digit and then
    reduced to the last four before anything is stored — see
    `app.services.employee_identifiers`.
    """

    pan: str | None = None
    uan: str | None = None
    aadhaar: str | None = None
    pf_number: str | None = None
    esi_number: str | None = None

    _pan = field_validator("pan", mode="before")(_identifier(ids.validate_pan))
    _uan = field_validator("uan", mode="before")(_identifier(ids.validate_uan))
    _aadhaar = field_validator("aadhaar", mode="before")(_identifier(ids.validate_aadhaar))
    _pf = field_validator("pf_number", mode="before")(_identifier(ids.validate_pf_number))
    _esi = field_validator("esi_number", mode="before")(_identifier(ids.validate_esi))

    @model_validator(mode="after")
    def _something(self) -> StatutoryDetails:
        if not self.model_fields_set:
            raise ValueError("Name at least one identifier to change.")
        return self


class BankAccountDetails(_Section):
    account_number: str
    ifsc: str
    account_holder_name: str = Field(min_length=1, max_length=255)
    bank_name: str | None = Field(default=None, max_length=128)

    _account = field_validator("account_number", mode="before")(
        _identifier(ids.validate_account_number)
    )
    _ifsc = field_validator("ifsc", mode="before")(_identifier(ids.validate_ifsc))
    _blank = field_validator("bank_name", mode="before")(_strip)

    @model_validator(mode="after")
    def _present(self) -> BankAccountDetails:
        if not self.account_number or not self.ifsc:
            raise ValueError("A bank account needs its number and IFSC.")
        return self


# ── Writes: one discriminated union over every section ─────────────────────


class IdentityWrite(BaseModel):
    section: Literal["identity"]
    data: IdentityDetails


class PersonalWrite(BaseModel):
    section: Literal["personal"]
    data: PersonalDetails


class AddressesWrite(BaseModel):
    section: Literal["addresses"]
    data: Addresses


class EmergencyContactsWrite(BaseModel):
    section: Literal["emergency_contacts"]
    data: list[EmergencyContact] = Field(max_length=5)

    @model_validator(mode="after")
    def _one_primary(self) -> EmergencyContactsWrite:
        if sum(1 for c in self.data if c.is_primary) > 1:
            raise ValueError("Mark one emergency contact as primary, not several.")
        return self


class FamilyMembersWrite(BaseModel):
    section: Literal["family_members"]
    data: list[FamilyMember] = Field(max_length=20)


class NomineesWrite(BaseModel):
    section: Literal["nominees"]
    data: list[Nominee] = Field(max_length=20)

    @model_validator(mode="after")
    def _shares(self) -> NomineesWrite:
        totals: dict[NomineePurpose, Decimal] = {}
        for nominee in self.data:
            totals[nominee.purpose] = totals.get(nominee.purpose, Decimal(0)) + (
                nominee.share_percent
            )
        wrong = {
            purpose.value.upper(): total for purpose, total in totals.items() if total != 100
        }
        if wrong:
            detail = ", ".join(f"{k} {v.normalize():f}%" for k, v in sorted(wrong.items()))
            raise ValueError(
                f"Nominee shares must total 100% for each purpose. Currently: {detail}."
            )
        return self


class EducationWrite(BaseModel):
    section: Literal["education"]
    data: list[Education] = Field(max_length=20)


class PreviousEmploymentWrite(BaseModel):
    section: Literal["previous_employment"]
    data: list[PreviousEmployment] = Field(max_length=20)


class StatutoryWrite(BaseModel):
    section: Literal["statutory"]
    data: StatutoryDetails


class BankAccountWrite(BaseModel):
    section: Literal["bank_account"]
    data: BankAccountDetails


SectionWrite = Annotated[
    IdentityWrite
    | PersonalWrite
    | AddressesWrite
    | EmergencyContactsWrite
    | FamilyMembersWrite
    | NomineesWrite
    | EducationWrite
    | PreviousEmploymentWrite
    | StatutoryWrite
    | BankAccountWrite,
    Field(discriminator="section"),
]


class ProfileUpdate(BaseModel):
    """A direct write of one section."""

    change: SectionWrite


class ChangeRequestCreate(BaseModel):
    change: SectionWrite
    reason: str = Field(min_length=3, max_length=1000)


class ChangeRequestDecision(BaseModel):
    note: str | None = Field(default=None, max_length=1000)


class ChangeRequestRejection(BaseModel):
    #: "Rejected" alone gives the employee nothing to fix.
    note: str = Field(min_length=3, max_length=1000)


# ── Reads ──────────────────────────────────────────────────────────────────


class StatutoryRead(BaseModel):
    """Masked for everyone except HR. Aadhaar is only ever its last four."""

    pan: str | None
    uan: str | None
    pf_number: str | None
    esi_number: str | None
    aadhaar: str | None
    masked: bool


class BankAccountSummary(BaseModel):
    id: uuid.UUID
    bank_name: str | None
    ifsc: str
    account_holder_name: str
    account_number_masked: str | None
    verification_method: str
    verified_at: datetime | None
    created_at: datetime


class ChangeRequestRead(BaseModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    employee_code: str
    employee_name: str
    section: ProfileSection
    section_label: str
    status: ProfileChangeStatus
    #: What is asked for and what was there when it was asked. Statutory
    #: values are masked for anyone outside HR; an account number is never in
    #: either, only its last four.
    proposed: dict[str, Any]
    current_snapshot: dict[str, Any]
    reason: str
    requested_at: datetime
    requested_by_name: str | None
    decided_at: datetime | None
    decided_by_name: str | None
    decision_note: str | None
    can_decide: bool
    can_withdraw: bool


class EmployeeProfileRead(BaseModel):
    employee_id: uuid.UUID
    personal: PersonalDetails
    addresses: Addresses
    emergency_contacts: list[EmergencyContact]
    family_members: list[FamilyMember]
    nominees: list[Nominee]
    education: list[Education]
    previous_employment: list[PreviousEmployment]
    statutory: StatutoryRead
    bank_account: BankAccountSummary | None
    pending_requests: list[ChangeRequestRead]
    #: HR only: what the offer carried in. Empty for everyone else.
    hiring_snapshot: dict[str, Any] | None
    #: What the viewer may write straight away, and what they may only request.
    direct_sections: list[ProfileSection]
    request_sections: list[ProfileSection]
    #: Plain-language gaps that block something downstream ("PAN — income tax
    #: cannot be computed without it"). Derived from the record, never guessed.
    missing: list[str]


class CelebrationRead(BaseModel):
    employee_id: uuid.UUID
    name: str
    designation: str | None
    photo_url: str | None
    kind: Literal["birthday", "work_anniversary"]
    on_date: date
    years: int | None
    #: `team` for a birthday shared only with the person's department.
    scope: str


class CelebrationsRead(BaseModel):
    from_date: date
    to_date: date
    items: list[CelebrationRead]
    note: str
