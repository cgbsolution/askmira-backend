"""Anonymisation that is actually anonymisation.

═══════════════════════════════════════════════════════════════════════════════
THE THING EVERYBODY GETS WRONG
═══════════════════════════════════════════════════════════════════════════════

Most "anonymisation" in HR systems is pseudonymisation with a nicer name.
`Priya Ramesh` becomes `Employee 4471`, everything else stays, and the record
still says: Senior Data Engineer, Bengaluru, grade M3, joined 14 March 2019,
₹42,00,000. There is exactly one of those. Anybody with the org chart can name
her in under a minute, and we told her the record was anonymous.

Three things have to be true and all three are easy to skip:

**1. The surrogate must not map back.** A dictionary from surrogate to employee
id kept "in case we need it" is a re-identification path, and its existence
means the data was never anonymised. The surrogate here is a keyed hash with a
per-tenant secret, and `verify_irreversible` asserts that no output carries any
input value — because the realistic failure is not a stored mapping, it is a
surrogate derived from something readable.

**2. Quasi-identifiers must be generalised, not kept.** Date of birth to a year.
Location to a city. Salary to a band. Designation to a job family. Each one is
harmless alone and jointly they are a fingerprint — the Sweeney result, that ZIP
plus birth date plus sex identifies most people, is not a curiosity, it is the
normal case in a 400-person company.

**3. A group of one is not anonymous.** After generalising, if a row is the only
one in its combination of quasi-identifiers, it identifies somebody as surely as
their name did. `check_k_anonymity` finds those, and the answer is to suppress
the row, not to ship it and call the set anonymous.

**And anonymisation is not deletion.** The row survives. Every string this
module produces for a person to read says so.

Pure. The tenant secret is supplied.
"""

from __future__ import annotations

import enum
import hashlib
import hmac
from collections import Counter
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

__all__ = [
    "DIRECT_IDENTIFIERS",
    "QUASI_IDENTIFIERS",
    "AnonymisationError",
    "AnonymisationResult",
    "KAnonymityReport",
    "anonymise_record",
    "check_k_anonymity",
    "generalise",
    "suppress_failing_groups",
    "surrogate_key",
    "verify_irreversible",
]


class AnonymisationError(ValueError):
    """An anonymisation this module will not claim to have performed."""


#: The default k. Five is the usual floor in disclosure-control practice and it
#: is a floor, not a target — a tenant may raise it, and `check_k_anonymity`
#: refuses a k below 2, because k=1 is the definition of not anonymous.
DEFAULT_K = 5


class Generalisation(str, enum.Enum):
    """How a quasi-identifier is blunted.

    Each one loses information on purpose. That is the trade: a birth year is
    less useful than a birth date and it is not a fingerprint.
    """

    YEAR_ONLY = "year_only"
    #: 2019 → "2015-2019". For a small population even a year can be a
    #: fingerprint when combined with a rare grade.
    FIVE_YEAR_BAND = "five_year_band"
    CITY_ONLY = "city_only"
    STATE_ONLY = "state_only"
    SALARY_BAND = "salary_band"
    #: "Senior Data Engineer" → "Engineering". Titles are close to unique.
    JOB_FAMILY = "job_family"
    #: Dropped entirely. For anything with no useful generalisation.
    SUPPRESS = "suppress"


#: Which fields are quasi-identifiers, and how each is generalised. A field not
#: in here and not a direct identifier passes through unchanged, which is safe
#: only because the caller says which fields it is handing over — and an
#: invariant checks the obvious ones are all listed.
QUASI_IDENTIFIERS: dict[str, Generalisation] = {
    "date_of_birth": Generalisation.YEAR_ONLY,
    "date_of_joining": Generalisation.FIVE_YEAR_BAND,
    "date_of_exit": Generalisation.FIVE_YEAR_BAND,
    "city": Generalisation.CITY_ONLY,
    "location": Generalisation.CITY_ONLY,
    "location_name": Generalisation.CITY_ONLY,
    "postal_code": Generalisation.SUPPRESS,
    "address": Generalisation.SUPPRESS,
    "designation": Generalisation.JOB_FAMILY,
    "ctc_annual": Generalisation.SALARY_BAND,
    "gross_pay": Generalisation.SALARY_BAND,
    "net_pay": Generalisation.SALARY_BAND,
    "basic": Generalisation.SALARY_BAND,
    "gender": Generalisation.SUPPRESS,
    "marital_status": Generalisation.SUPPRESS,
    "blood_group": Generalisation.SUPPRESS,
}

#: Fields whose value identifies somebody outright. Replaced by the surrogate
#: or dropped; never generalised, because there is no useful generalisation of
#: an Aadhaar number.
DIRECT_IDENTIFIERS: frozenset[str] = frozenset(
    {
        "full_name",
        "first_name",
        "last_name",
        "display_name",
        "employee_code",
        "work_email",
        "personal_email",
        "email",
        "phone",
        "mobile",
        "pan",
        "aadhaar_last4",
        "uan",
        "esi_number",
        "pf_number",
        "passport_number",
        "bank_account_number",
        "bank_ifsc",
        "photo_url",
        "ip_address",
        "user_agent",
        "device_id",
    }
)

#: Salary bands, in rupees per annum. Wide on purpose: a band of ±50,000 around
#: a real figure is a salary, and calling it a band does not change that.
_SALARY_BANDS: tuple[tuple[Decimal, str], ...] = (
    (Decimal("300000"), "Under 3L"),
    (Decimal("600000"), "3L-6L"),
    (Decimal("1000000"), "6L-10L"),
    (Decimal("1500000"), "10L-15L"),
    (Decimal("2500000"), "15L-25L"),
    (Decimal("4000000"), "25L-40L"),
    (Decimal("6000000"), "40L-60L"),
)
_TOP_BAND = "Over 60L"

#: Title → family. Deliberately coarse; a family with one person in it has done
#: nothing for anonymity, and the k-check will catch that anyway.
_JOB_FAMILIES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("engineer", "developer", "sre", "architect", "qa", "tester"), "Engineering"),
    (("designer", "ux", "ui", "researcher"), "Design"),
    (("product", "programme", "program", "project", "scrum"), "Product"),
    (("sales", "account executive", "business development", "bd "), "Sales"),
    (("marketing", "brand", "content", "growth"), "Marketing"),
    (("hr", "people", "talent", "recruit"), "People"),
    (("finance", "account", "payroll", "audit", "tax"), "Finance"),
    (("support", "success", "service desk"), "Customer Support"),
    (("operations", "admin", "facilit", "logistics"), "Operations"),
    (("legal", "compliance", "secretar"), "Legal and Compliance"),
    (("director", "head", "vp", "chief", "president"), "Leadership"),
)


def surrogate_key(value: str, *, tenant_secret: str) -> str:
    """A stable surrogate that does not map back.

    HMAC-SHA256 keyed on a per-tenant secret, truncated to 16 hex characters.
    Stable, so the same person anonymised in two tables joins to themselves and
    the aggregates still work. Not reversible, so the join does not reach a
    name.

    The security rests on the secret, not on the hash: a plain SHA-256 of an
    employee id is trivially reversed by hashing every id in the table, and that
    is the mistake this function exists to prevent somebody making inline.

    **The secret must never be stored beside the anonymised rows.** If it is,
    the pair is a mapping table with extra steps.
    """
    if not tenant_secret or len(tenant_secret) < 32:
        raise AnonymisationError(
            "The anonymisation secret has to be at least 32 characters. A short "
            "or empty key makes the surrogate reversible by brute force, and "
            "reversible means the record was never anonymised."
        )
    digest = hmac.new(
        tenant_secret.encode("utf-8"), value.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"anon-{digest[:16]}"


def _salary_band(value: Any) -> str:
    try:
        amount = Decimal(str(value))
    except Exception:
        # Any unparseable value becomes "unknown" rather than passing through.
        # A salary that failed to parse must not survive as its original string.
        return "unknown"
    for ceiling, label in _SALARY_BANDS:
        if amount < ceiling:
            return label
    return _TOP_BAND


def _job_family(value: Any) -> str:
    text = str(value or "").lower()
    for needles, family in _JOB_FAMILIES:
        if any(needle in text for needle in needles):
            return family
    return "Other"


def _five_year_band(value: date) -> str:
    start = value.year - (value.year % 5)
    return f"{start}-{start + 4}"


def generalise(field: str, value: Any) -> Any:
    """Blunt one quasi-identifier.

    Returns `None` for a suppressed field. A field with no generalisation rule
    is returned unchanged — the caller decides what it hands over, and
    `verify_irreversible` is what catches a direct identifier slipping through
    that way.
    """
    rule = QUASI_IDENTIFIERS.get(field)
    if rule is None:
        return value
    if value is None:
        return None

    if rule is Generalisation.SUPPRESS:
        return None
    if rule is Generalisation.YEAR_ONLY:
        return value.year if isinstance(value, date) else None
    if rule is Generalisation.FIVE_YEAR_BAND:
        return _five_year_band(value) if isinstance(value, date) else None
    if rule is Generalisation.CITY_ONLY:
        # Already a city; the generalisation is that nothing finer survives.
        return str(value)
    if rule is Generalisation.STATE_ONLY:
        return str(value)
    if rule is Generalisation.SALARY_BAND:
        return _salary_band(value)
    if rule is Generalisation.JOB_FAMILY:
        return _job_family(value)
    return value


@dataclass(frozen=True, slots=True)
class AnonymisationResult:
    """One anonymised record, and an account of what was done to it."""

    record: dict[str, Any]
    surrogate: str
    direct_identifiers_removed: tuple[str, ...]
    fields_generalised: tuple[str, ...]
    fields_suppressed: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "record": self.record,
            "surrogate": self.surrogate,
            "direct_identifiers_removed": list(self.direct_identifiers_removed),
            "fields_generalised": list(self.fields_generalised),
            "fields_suppressed": list(self.fields_suppressed),
            "note": (
                "This record still exists. It has been anonymised, not deleted: "
                "the identifying values are gone and the row and its totals "
                "remain."
            ),
        }


def anonymise_record(
    record: dict[str, Any],
    *,
    subject_id: str,
    tenant_secret: str,
) -> AnonymisationResult:
    """Turn one record about a person into one record about nobody.

    Direct identifiers go. Quasi-identifiers are generalised. Everything else
    passes through — which is the right default only because the caller chooses
    what it passes in, and because `verify_irreversible` checks the output
    afterwards rather than trusting this function to have been complete.
    """
    surrogate = surrogate_key(subject_id, tenant_secret=tenant_secret)

    out: dict[str, Any] = {"subject": surrogate}
    removed: list[str] = []
    generalised: list[str] = []
    suppressed: list[str] = []

    for field, value in record.items():
        if field in DIRECT_IDENTIFIERS:
            removed.append(field)
            continue
        if field in QUASI_IDENTIFIERS:
            new_value = generalise(field, value)
            if new_value is None and value is not None:
                suppressed.append(field)
                continue
            out[field] = new_value
            generalised.append(field)
            continue
        out[field] = value

    return AnonymisationResult(
        record=out,
        surrogate=surrogate,
        direct_identifiers_removed=tuple(removed),
        fields_generalised=tuple(generalised),
        fields_suppressed=tuple(suppressed),
    )


def verify_irreversible(
    *,
    original: dict[str, Any],
    result: AnonymisationResult,
    subject_id: str,
) -> None:
    """Refuse to call something anonymised when it is not.

    Checks the output rather than trusting the transformation:

    - no direct identifier's value survives anywhere in the output, under any
      key — including inside the surrogate, which is the realistic mistake
      (somebody derives it from the email "so it is stable");
    - the subject id itself does not appear;
    - a date of birth has not survived as a full date.

    Raises rather than returning a report, because a caller that could ignore
    the report would ship the record. There is no partial pass here: either the
    output is anonymous or it is not, and the second one must not be written.
    """
    haystack = " ".join(str(v).lower() for v in result.record.values() if v is not None)
    haystack = f"{haystack} {result.surrogate.lower()}"

    leaks: list[str] = []

    for field in DIRECT_IDENTIFIERS:
        value = original.get(field)
        if value is None:
            continue
        text = str(value).strip().lower()
        # Very short values produce false positives (a single-letter middle
        # name matches everything), and are not identifying on their own.
        if len(text) < 4:
            continue
        if text in haystack:
            leaks.append(f"{field} ({text!r}) survives in the output")

    if str(subject_id).lower() in haystack:
        leaks.append(
            "the subject id survives in the output, so every row joins straight back to the person"
        )

    dob = original.get("date_of_birth")
    if isinstance(dob, date) and result.record.get("date_of_birth") == dob:
        leaks.append("date_of_birth survives as a full date rather than a year")

    if leaks:
        raise AnonymisationError(
            "This record is not anonymised and must not be stored as though it "
            "were:\n  - " + "\n  - ".join(leaks)
        )


@dataclass(frozen=True, slots=True)
class KAnonymityReport:
    """Whether a set of anonymised records is actually anonymous.

    `singletons` is the number that matters. One row alone in its combination of
    quasi-identifiers identifies somebody as surely as their name did, and a set
    containing one is not an anonymous set — it is an anonymous set plus a
    person.
    """

    k: int
    total_records: int
    #: Groups whose size is below k, as (quasi-identifier values, count).
    failing_groups: tuple[tuple[tuple[Any, ...], int], ...]
    quasi_identifiers_used: tuple[str, ...]

    @property
    def singletons(self) -> int:
        return sum(1 for _values, count in self.failing_groups if count == 1)

    @property
    def records_at_risk(self) -> int:
        return sum(count for _values, count in self.failing_groups)

    @property
    def is_anonymous(self) -> bool:
        return not self.failing_groups

    def as_dict(self) -> dict[str, Any]:
        return {
            "k": self.k,
            "total_records": self.total_records,
            "is_anonymous": self.is_anonymous,
            "failing_group_count": len(self.failing_groups),
            "singletons": self.singletons,
            "records_at_risk": self.records_at_risk,
            "quasi_identifiers_used": list(self.quasi_identifiers_used),
            "recommendation": (
                "Suppress the records in the failing groups. Publishing them "
                "and calling the set anonymous would be publishing "
                f"{self.records_at_risk} record(s) that identify somebody."
                if not self.is_anonymous
                else "Every group has at least k records."
            ),
        }


def check_k_anonymity(
    records: list[dict[str, Any]],
    *,
    k: int = DEFAULT_K,
    quasi_identifiers: tuple[str, ...] = (),
) -> KAnonymityReport:
    """Find the groups too small to hide anybody.

    A record that is the only one in its department, grade and salary band is
    not anonymous, and this is the check that says so rather than the comment
    that hopes so.
    """
    if k < 2:
        raise AnonymisationError(
            f"k={k} is not an anonymity threshold. k=1 means every record is "
            "alone in its group, which is the definition of identifiable."
        )

    fields = quasi_identifiers or tuple(
        sorted(
            {
                field
                for record in records
                for field in record
                if field in QUASI_IDENTIFIERS or field in ("department", "grade")
            }
        )
    )

    groups = Counter(tuple(record.get(field) for field in fields) for record in records)
    failing = tuple(
        sorted(
            ((values, count) for values, count in groups.items() if count < k),
            key=lambda pair: pair[1],
        )
    )

    return KAnonymityReport(
        k=k,
        total_records=len(records),
        failing_groups=failing,
        quasi_identifiers_used=fields,
    )


def suppress_failing_groups(
    records: list[dict[str, Any]],
    *,
    k: int = DEFAULT_K,
    quasi_identifiers: tuple[str, ...] = (),
) -> tuple[list[dict[str, Any]], KAnonymityReport]:
    """Drop the records that would identify somebody, and say how many.

    Suppression rather than further generalisation: generalising until a
    singleton has company means blunting every other record too, and a dataset
    generalised to "Engineering, some year, some band" answers no question
    anybody had. Dropping the handful that cannot be hidden keeps the rest
    useful and the claim true.
    """
    report = check_k_anonymity(records, k=k, quasi_identifiers=quasi_identifiers)
    if report.is_anonymous:
        return list(records), report

    failing_values = {values for values, _count in report.failing_groups}
    kept = [
        record
        for record in records
        if tuple(record.get(f) for f in report.quasi_identifiers_used) not in failing_values
    ]
    return kept, report
