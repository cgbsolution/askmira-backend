"""What kinds of personal data we hold, and on what lawful basis.

═══════════════════════════════════════════════════════════════════════════════
CONSENT IS NOT THE DEFAULT BASIS, AND ASKING FOR IT WHERE IT IS NOT IS A BUG
═══════════════════════════════════════════════════════════════════════════════

The commonest way to get DPDP wrong in an HRMS is to put a consent checkbox in
front of everything. It looks careful and it is the opposite: consent is
withdrawable, so asking for consent to process payroll implies payroll can be
switched off on request, and it cannot — Form 24Q has to be filed whether or not
the employee would rather it were not.

So `lawful_basis` is declared per purpose, and `consent.py` refuses to record a
consent decision for a purpose whose basis is not consent. The checkbox only
exists where the answer can genuinely be no.

═══════════════════════════════════════════════════════════════════════════════
A NOTE ON "SENSITIVE PERSONAL DATA"
═══════════════════════════════════════════════════════════════════════════════

The DPDP Act does **not** carry over the SPDI Rules 2011 tiering — it does not
define a "sensitive personal data" class with heavier statutory duties. What it
does carry is a duty of reasonable security safeguards over all personal data,
plus specific treatment for children and persons with disability.

`is_sensitive_personal_data` on a category is therefore **our own handling
flag**, not a statutory tier: it drives who can read the field, whether it is
redacted in exports and audit payloads, and whether an AI call may include it.
Calling it a statutory classification in a compliance surface would be
misleading, so the tenant-facing wording says "extra handling", and this
paragraph exists so nobody later mistakes the flag for a legal conclusion.

Pure.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any

__all__ = [
    "CATEGORIES",
    "HIGH_HARM_CATEGORIES",
    "DataCategory",
    "LawfulBasis",
    "PrincipalKind",
    "Retention",
    "category",
    "category_keys",
]


class LawfulBasis(str, enum.Enum):
    """Why we are allowed to process this at all.

    Modelled on DPDP s.4 (consent or certain legitimate uses) and read together
    with the employment relationship. The distinction that matters operationally
    is whether the principal can say no and have processing stop.
    """

    #: DPDP s.6. Freely given, specific, informed, unconditional, unambiguous —
    #: and withdrawable with the same ease it was given.
    CONSENT = "consent"
    #: Necessary to perform the contract of employment. Paying somebody is not
    #: optional processing; it is the thing they signed up for.
    EMPLOYMENT_CONTRACT = "employment_contract"
    #: Compelled by statute — EPF, ESI, TDS, Shops & Establishments registers,
    #: POSH records. Cannot be withdrawn and must not be presented as though it
    #: could.
    LEGAL_OBLIGATION = "legal_obligation"
    #: DPDP s.7 legitimate uses: the principal voluntarily provided it for a
    #: purpose and has not indicated otherwise, employment-related safeguarding
    #: of the employer against loss or liability, and similar.
    LEGITIMATE_USE = "legitimate_use"
    #: Medical emergency, threat to life. Rare here, real in an emergency
    #: contact record.
    VITAL_INTEREST = "vital_interest"

    @property
    def is_withdrawable(self) -> bool:
        """Whether the principal can stop this by asking.

        Only consent. This property is the one place that question is answered,
        because a second implementation of it would eventually disagree.
        """
        return self is LawfulBasis.CONSENT

    @property
    def label(self) -> str:
        return {
            LawfulBasis.CONSENT: "Your consent",
            LawfulBasis.EMPLOYMENT_CONTRACT: "Performance of your employment contract",
            LawfulBasis.LEGAL_OBLIGATION: "A legal obligation on your employer",
            LawfulBasis.LEGITIMATE_USE: "A legitimate use under section 7",
            LawfulBasis.VITAL_INTEREST: "Protection of life or health",
        }[self]


class PrincipalKind(str, enum.Enum):
    """Who a row is about.

    A data-principal request has to reach every row about that person, and a
    row's principal is not always the employee it is filed under: a nominee
    record is about the nominee, and they have their own rights over it even
    though they will never log in.
    """

    EMPLOYEE = "employee"
    CANDIDATE = "candidate"
    #: Family members on insurance, PF nomination, gratuity nomination.
    DEPENDENT = "dependent"
    EMERGENCY_CONTACT = "emergency_contact"
    #: A person who has a login but may not be an employee of the tenant —
    #: an external interviewer, an implementation consultant.
    PLATFORM_USER = "platform_user"
    #: A named person at a vendor, or a reference. Held incidentally, still
    #: personal data.
    THIRD_PARTY = "third_party"


@dataclass(frozen=True, slots=True)
class Retention:
    """How long a category is kept, and what makes that a floor rather than a choice.

    `statutory_reference` being set is what turns "we keep this for seven years"
    from a policy into an obligation, and it is what an erasure response cites
    when it tells somebody why part of their record survived their request.
    `months` with no reference is our own policy and a tenant may shorten it;
    `months` with one, they may not.
    """

    months: int
    statutory_reference: str | None = None
    #: What the clock runs from. Almost never the creation date — payroll
    #: retention runs from the end of the financial year, employment records
    #: from the last working day.
    clock_starts: str = "record_created"
    note: str = ""

    @property
    def is_statutory(self) -> bool:
        return self.statutory_reference is not None


@dataclass(frozen=True, slots=True)
class DataCategory:
    """One kind of personal data, with everything the RoPA needs about it."""

    key: str
    name: str
    description: str
    principal_kinds: tuple[PrincipalKind, ...]
    lawful_basis: LawfulBasis
    #: Purpose keys from `consent.PURPOSES`. Checked for existence by an
    #: invariant, so a category cannot cite a purpose nobody defined.
    purposes: tuple[str, ...]
    retention: Retention
    #: Our own handling flag, not a statutory tier. See the module docstring.
    is_sensitive_personal_data: bool = False
    #: Whether this may be sent to an AI model provider. Read by the agent
    #: layer and by `transfers.py`; a category marked False here must never
    #: appear in a prompt.
    may_leave_for_ai: bool = False
    examples: tuple[str, ...] = ()

    @property
    def retention_period_months(self) -> int:
        return self.retention.months

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "description": self.description,
            "principal_kinds": [p.value for p in self.principal_kinds],
            "lawful_basis": self.lawful_basis.value,
            "lawful_basis_label": self.lawful_basis.label,
            "can_be_withdrawn": self.lawful_basis.is_withdrawable,
            "purposes": list(self.purposes),
            "retention_period_months": self.retention.months,
            "retention_is_statutory": self.retention.is_statutory,
            "statutory_reference": self.retention.statutory_reference,
            "retention_clock_starts": self.retention.clock_starts,
            "retention_note": self.retention.note,
            "requires_extra_handling": self.is_sensitive_personal_data,
            "may_leave_for_ai": self.may_leave_for_ai,
            "examples": list(self.examples),
        }


def _c(**kwargs: Any) -> DataCategory:
    return DataCategory(**kwargs)


#: The catalogue. Every classified column in the schema resolves to exactly one
#: of these, and the Record of Processing Activities is generated from them —
#: which is why the lawful basis and the retention live here and not in a
#: document somebody has to remember to update.
#:
#: **Every lawful_basis and every statutory_reference below is an engineering
#: draft awaiting counsel's confirmation.** They are the interpretations this
#: build proceeds on, written down so they can be argued with.
CATEGORIES: dict[str, DataCategory] = {
    c.key: c
    for c in (
        _c(
            key="identity",
            name="Identity and contact",
            description=(
                "Who somebody is and how to reach them: name, personal and work "
                "email, phone, postal address, date of birth, gender."
            ),
            principal_kinds=(
                PrincipalKind.EMPLOYEE,
                PrincipalKind.CANDIDATE,
                PrincipalKind.PLATFORM_USER,
            ),
            lawful_basis=LawfulBasis.EMPLOYMENT_CONTRACT,
            purposes=("employment_administration",),
            retention=Retention(
                months=96,
                statutory_reference="Shops & Establishments (state) employee register",
                clock_starts="last_working_day",
                note=(
                    "Eight years is the longest of the several state registers we "
                    "have to satisfy; a tenant operating in one state only may be "
                    "able to shorten it, which is a question for their counsel."
                ),
            ),
            examples=("full_name", "personal_email", "phone", "date_of_birth"),
        ),
        _c(
            key="statutory_identifier",
            name="Statutory identifiers",
            description=(
                "PAN, Aadhaar, UAN, ESIC number, passport. Government-issued "
                "numbers that identify a person across every system they appear in."
            ),
            principal_kinds=(PrincipalKind.EMPLOYEE, PrincipalKind.DEPENDENT),
            lawful_basis=LawfulBasis.LEGAL_OBLIGATION,
            purposes=("statutory_filing", "employment_administration"),
            retention=Retention(
                months=84,
                statutory_reference="Income-tax Act s.44AA and Rule 6F; EPF Act s.5",
                clock_starts="end_of_financial_year",
            ),
            is_sensitive_personal_data=True,
            examples=("pan_number", "aadhaar_number", "uan", "esic_number"),
        ),
        _c(
            key="financial",
            name="Bank and payment details",
            description="Bank account number, IFSC, UPI handle, cancelled cheque.",
            principal_kinds=(PrincipalKind.EMPLOYEE,),
            lawful_basis=LawfulBasis.EMPLOYMENT_CONTRACT,
            purposes=("payroll_disbursement",),
            retention=Retention(
                months=84,
                statutory_reference="Income-tax Act Rule 6F (books and vouchers)",
                clock_starts="end_of_financial_year",
            ),
            is_sensitive_personal_data=True,
            examples=("bank_account_number", "ifsc_code"),
        ),
        _c(
            key="compensation",
            name="Compensation",
            description=(
                "CTC, salary structure, revisions, variable pay, the pay register "
                "and every payslip generated from it."
            ),
            principal_kinds=(PrincipalKind.EMPLOYEE, PrincipalKind.CANDIDATE),
            lawful_basis=LawfulBasis.EMPLOYMENT_CONTRACT,
            purposes=("payroll_processing", "statutory_filing"),
            retention=Retention(
                months=96,
                statutory_reference=(
                    "Payment of Wages Act s.13A; Income-tax Act Rule 6F; "
                    "EPF Act s.5 read with para 76"
                ),
                clock_starts="end_of_financial_year",
                note=(
                    "This is the category that most often survives an erasure "
                    "request, and the response has to say so precisely."
                ),
            ),
            is_sensitive_personal_data=True,
            examples=("ctc_annual", "gross_pay", "net_pay", "basic"),
        ),
        _c(
            key="tax",
            name="Tax declarations and proofs",
            description=(
                "Regime election, investment declarations, rent receipts, landlord "
                "PAN, submitted proof documents and Form 16."
            ),
            principal_kinds=(PrincipalKind.EMPLOYEE, PrincipalKind.THIRD_PARTY),
            lawful_basis=LawfulBasis.LEGAL_OBLIGATION,
            purposes=("statutory_filing",),
            retention=Retention(
                months=84,
                statutory_reference="Income-tax Act s.201 and Rule 31A",
                clock_starts="end_of_financial_year",
            ),
            is_sensitive_personal_data=True,
        ),
        _c(
            key="attendance",
            name="Attendance and location",
            description=(
                "Punches, geofence coordinates captured at punch, device "
                "identifiers, shift assignment, regularisation reasons."
            ),
            principal_kinds=(PrincipalKind.EMPLOYEE,),
            lawful_basis=LawfulBasis.EMPLOYMENT_CONTRACT,
            purposes=("attendance_and_leave", "payroll_processing"),
            retention=Retention(
                months=36,
                statutory_reference=(
                    "Shops & Establishments (state) attendance register; "
                    "Factories Act s.62 where applicable"
                ),
                clock_starts="record_created",
            ),
            is_sensitive_personal_data=True,
            examples=("punch_latitude", "punch_longitude", "device_id"),
        ),
        _c(
            key="leave",
            name="Leave",
            description="Balances, applications, approvals and the reasons given.",
            principal_kinds=(PrincipalKind.EMPLOYEE,),
            lawful_basis=LawfulBasis.EMPLOYMENT_CONTRACT,
            purposes=("attendance_and_leave",),
            retention=Retention(
                months=36,
                statutory_reference="Shops & Establishments (state) leave register",
                clock_starts="record_created",
                note=(
                    "A leave reason can disclose health or a family circumstance the "
                    "person never intended their whole reporting line to read. The "
                    "reason is treated as extra-handling even though a balance is not."
                ),
            ),
            is_sensitive_personal_data=True,
        ),
        _c(
            key="health",
            name="Health and accessibility",
            description=(
                "Medical certificates, insurance nominations, disability status, "
                "blood group, fitness certificates."
            ),
            principal_kinds=(PrincipalKind.EMPLOYEE, PrincipalKind.DEPENDENT),
            lawful_basis=LawfulBasis.LEGAL_OBLIGATION,
            purposes=("statutory_filing", "employee_benefits"),
            retention=Retention(
                months=60,
                statutory_reference=(
                    "ESI Act s.46; Rights of Persons with Disabilities Act"
                ),
                clock_starts="last_working_day",
                note=(
                    "DPDP requires a verifiable-consent route for a person with a "
                    "disability who has a lawful guardian. That route is not built."
                ),
            ),
            is_sensitive_personal_data=True,
        ),
        _c(
            key="performance",
            name="Performance and appraisal",
            description=(
                "Ratings, calibration outcomes, goals, manager and peer feedback, "
                "PIP records."
            ),
            principal_kinds=(PrincipalKind.EMPLOYEE,),
            lawful_basis=LawfulBasis.EMPLOYMENT_CONTRACT,
            purposes=("performance_management",),
            retention=Retention(
                months=60,
                clock_starts="last_working_day",
                note=(
                    "Our own policy, not a statute: long enough to defend a "
                    "termination decision, short enough that a rating from two jobs "
                    "ago is not still following somebody."
                ),
            ),
            is_sensitive_personal_data=True,
        ),
        _c(
            key="culture_response",
            name="Culture survey responses",
            description=(
                "Pulse and engagement responses, eNPS, sentiment derived from free "
                "text, and the anonymity threshold that governs their release."
            ),
            principal_kinds=(PrincipalKind.EMPLOYEE,),
            lawful_basis=LawfulBasis.CONSENT,
            purposes=("culture_analytics",),
            retention=Retention(
                months=24,
                clock_starts="record_created",
                note=(
                    "A survey answered on a promise of anonymity, retained long "
                    "enough to be de-anonymised by a shrinking team, is a broken "
                    "promise on a delay."
                ),
            ),
            is_sensitive_personal_data=True,
            may_leave_for_ai=True,
        ),
        _c(
            key="recruitment",
            name="Candidate and application data",
            description=(
                "CV, application, screening notes, interview feedback, assessment "
                "scores, offer terms."
            ),
            principal_kinds=(PrincipalKind.CANDIDATE,),
            lawful_basis=LawfulBasis.LEGITIMATE_USE,
            purposes=("recruitment",),
            retention=Retention(
                months=12,
                clock_starts="application_closed",
                note=(
                    "DPDP s.7(a): the candidate voluntarily provided it for this "
                    "purpose. Keeping it for a future role is a different purpose "
                    "and needs consent — see the talent_pool purpose."
                ),
            ),
            may_leave_for_ai=True,
        ),
        _c(
            key="background_check",
            name="Background verification",
            description=(
                "Employment verification, education verification, criminal record "
                "checks and the vendor's report."
            ),
            principal_kinds=(PrincipalKind.CANDIDATE, PrincipalKind.EMPLOYEE),
            lawful_basis=LawfulBasis.CONSENT,
            purposes=("background_verification",),
            retention=Retention(
                months=36,
                clock_starts="last_working_day",
                note=(
                    "Our own policy, not a statute. Long enough to answer a "
                    "challenge to a hiring decision the check informed; short "
                    "enough that a discrepancy found once is not re-surfaced "
                    "against somebody for the rest of their career."
                ),
            ),
            is_sensitive_personal_data=True,
        ),
        _c(
            key="disciplinary",
            name="Disciplinary and grievance",
            description=(
                "Warnings, enquiry records, POSH complaints and their outcomes, "
                "exit-interview statements."
            ),
            principal_kinds=(PrincipalKind.EMPLOYEE,),
            lawful_basis=LawfulBasis.LEGAL_OBLIGATION,
            purposes=("employment_administration", "statutory_filing"),
            retention=Retention(
                months=60,
                statutory_reference=(
                    "POSH Act s.21 and Rule 14 (annual report and records)"
                ),
                clock_starts="case_closed",
                note=(
                    "A POSH record names a complainant as well as a respondent, and "
                    "an erasure request from either one cannot delete the other's "
                    "account of what happened."
                ),
            ),
            is_sensitive_personal_data=True,
        ),
        _c(
            key="dependent",
            name="Family and nominees",
            description=(
                "Nominees for PF and gratuity, insurance dependants, emergency "
                "contacts — people who are not our users and never consented to us."
            ),
            principal_kinds=(
                PrincipalKind.DEPENDENT,
                PrincipalKind.EMERGENCY_CONTACT,
            ),
            lawful_basis=LawfulBasis.LEGAL_OBLIGATION,
            purposes=("employee_benefits", "statutory_filing"),
            retention=Retention(
                months=84,
                statutory_reference=(
                    "EPF Scheme para 61 (nomination); Payment of Gratuity Rules r.6"
                ),
                clock_starts="last_working_day",
            ),
            is_sensitive_personal_data=True,
        ),
        _c(
            key="documents",
            name="Uploaded documents",
            description=(
                "Anything the person or HR attached: ID scans, certificates, "
                "offer letters, relieving letters, signed policies."
            ),
            principal_kinds=(PrincipalKind.EMPLOYEE, PrincipalKind.CANDIDATE),
            lawful_basis=LawfulBasis.EMPLOYMENT_CONTRACT,
            purposes=("employment_administration",),
            retention=Retention(
                months=96,
                clock_starts="last_working_day",
                note=(
                    "Matched to the employment record it evidences: a relieving "
                    "letter outliving the record it refers to, or the reverse, "
                    "leaves one of them unverifiable."
                ),
            ),
            is_sensitive_personal_data=True,
        ),
        _c(
            key="communication",
            name="Notifications and messages",
            description=(
                "Emails, WhatsApp and in-app messages sent to a person, their "
                "delivery state, and the channel preferences they set."
            ),
            principal_kinds=(
                PrincipalKind.EMPLOYEE,
                PrincipalKind.CANDIDATE,
                PrincipalKind.PLATFORM_USER,
            ),
            lawful_basis=LawfulBasis.EMPLOYMENT_CONTRACT,
            purposes=("employment_administration", "marketing_communication"),
            retention=Retention(
                months=24,
                clock_starts="record_created",
                note=(
                    "Our own policy. Two years covers the appraisal and "
                    "increment cycles a message might be evidence in; keeping "
                    "delivery logs longer builds a picture of somebody's working "
                    "hours nobody asked for."
                ),
            ),
        ),
        _c(
            key="account_security",
            name="Account and security",
            description=(
                "Password hashes, session and refresh tokens, MFA enrolment, login "
                "attempts, IP addresses and user agents."
            ),
            principal_kinds=(PrincipalKind.PLATFORM_USER, PrincipalKind.EMPLOYEE),
            lawful_basis=LawfulBasis.LEGITIMATE_USE,
            purposes=("security_and_fraud",),
            retention=Retention(
                months=18,
                clock_starts="record_created",
                note=(
                    "DPDP s.7(i): prevention of fraud and network security. Login "
                    "history is also the evidence trail a breach investigation runs "
                    "on, which is why it outlives the session by a long way."
                ),
            ),
            is_sensitive_personal_data=True,
        ),
        _c(
            key="audit_trail",
            name="Audit trail",
            description=(
                "Who changed what, when. Insert-only, and referenced by every "
                "compliance answer this product gives."
            ),
            principal_kinds=(PrincipalKind.PLATFORM_USER, PrincipalKind.EMPLOYEE),
            lawful_basis=LawfulBasis.LEGAL_OBLIGATION,
            purposes=("security_and_fraud", "statutory_filing"),
            retention=Retention(
                months=96,
                statutory_reference=(
                    "IT Act s.43A read with the reasonable-security duty"
                ),
                clock_starts="record_created",
                note=(
                    "The audit log cannot be erased on request without destroying "
                    "the record of the erasure itself. What it holds about a person "
                    "is pseudonymised on erasure instead — actor id retained, "
                    "identifying payload dropped."
                ),
            ),
        ),
        _c(
            key="ai_interaction",
            name="AI interactions",
            description=(
                "Prompts, retrieved context, model outputs, and the human decision "
                "recorded on each — including which model provider saw what."
            ),
            principal_kinds=(PrincipalKind.EMPLOYEE, PrincipalKind.PLATFORM_USER),
            lawful_basis=LawfulBasis.CONSENT,
            purposes=("ai_assistance",),
            retention=Retention(
                months=12,
                clock_starts="record_created",
                note=(
                    "Kept so an AI decision can be explained after the fact, which "
                    "is the only reason to keep it. The transfer register in "
                    "transfers.py records where each of these went."
                ),
            ),
            is_sensitive_personal_data=True,
            may_leave_for_ai=True,
        ),
        _c(
            key="governance_record",
            name="Your privacy decisions and requests",
            description=(
                "The consents you gave or withdrew, the notice you were shown "
                "when you decided, and any access, correction, erasure or "
                "grievance request you made — with what we did about it."
            ),
            principal_kinds=(
                PrincipalKind.EMPLOYEE,
                PrincipalKind.CANDIDATE,
                PrincipalKind.PLATFORM_USER,
            ),
            lawful_basis=LawfulBasis.LEGAL_OBLIGATION,
            purposes=("dpdp_compliance",),
            retention=Retention(
                months=84,
                statutory_reference="DPDP Act s.8(4) — demonstrable compliance",
                clock_starts="record_created",
                note=(
                    "Kept longer than the consent it records, because the "
                    "obligation is to be able to show what was asked and what "
                    "was answered. An erasure request cannot delete the record "
                    "of the erasure request — that is the one deletion that "
                    "would destroy the evidence it was honoured."
                ),
            ),
        ),
        _c(
            key="organisational",
            name="Organisational placement",
            description=(
                "Department, designation, grade, location, reporting line, "
                "employment status and the lifecycle events that changed them."
            ),
            principal_kinds=(PrincipalKind.EMPLOYEE,),
            lawful_basis=LawfulBasis.EMPLOYMENT_CONTRACT,
            purposes=("employment_administration",),
            retention=Retention(
                months=96,
                statutory_reference="Shops & Establishments (state) employee register",
                clock_starts="last_working_day",
            ),
        ),
    )
}


def category(key: str) -> DataCategory:
    """Look one up, or say which one was meant and does not exist."""
    try:
        return CATEGORIES[key]
    except KeyError:
        raise KeyError(
            f"No data category {key!r}. Known categories: "
            f"{', '.join(sorted(CATEGORIES))}."
        ) from None


def category_keys() -> frozenset[str]:
    return frozenset(CATEGORIES)


#: Kept out of `DataCategory` because it is a property of a whole incident, not
#: of a field: which categories, if any of them are involved, make a breach
#: notifiable to affected principals rather than only to the Board.
HIGH_HARM_CATEGORIES: frozenset[str] = frozenset(
    {
        "statutory_identifier",
        "financial",
        "health",
        "disciplinary",
        "account_security",
        "background_check",
    }
)
