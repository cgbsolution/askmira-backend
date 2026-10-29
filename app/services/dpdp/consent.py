"""Consent: only where consent is genuinely the basis, and withdrawable when it is.

═══════════════════════════════════════════════════════════════════════════════
THE CHECKBOX THAT LIES
═══════════════════════════════════════════════════════════════════════════════

An HRMS that shows "I consent to my salary being processed" is telling the
employee something false. They cannot withdraw it. If they tick no, they still
get paid, TDS is still deducted, Form 24Q is still filed with their PAN on it —
because the basis is the employment contract and a statutory obligation, not
their permission. Presenting it as consent means the first person who withdraws
discovers the tick box was decorative, and they are right to be angry about it.

So this module makes the mistake unrepresentable rather than discouraged:

- Every purpose declares its `lawful_basis` (from `categories.LawfulBasis`).
- `record_decision` **raises** for a purpose whose basis is not CONSENT. There
  is no flag to override it. A caller that wants a consent row for payroll has
  a design error, and it surfaces at the call site instead of in a DPO's inbox.
- `withdraw` refuses for `is_mandatory_for_employment` purposes — but note that
  a mandatory purpose can never have CONSENT as its basis in the first place
  (an invariant checks this), so that guard is a second line, not the first.

═══════════════════════════════════════════════════════════════════════════════
WITHDRAWAL HAS TO ACTUALLY STOP SOMETHING
═══════════════════════════════════════════════════════════════════════════════

DPDP s.6(6): withdrawing consent must be as easy as giving it. The easy half is
a button. The hard half is that processing then stops, and `Purpose.stops`
names — in code, not in prose — the specific capabilities that switch off. The
service reads that list; `is_permitted` is the single question every dependent
feature asks before it runs.

A purpose whose withdrawal stops nothing would be a purpose we should not have
asked about, so an invariant requires every consent-based purpose to name at
least one capability.

Pure. `now` is always supplied.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.services.dpdp.categories import LawfulBasis

__all__ = [
    "CAPABILITIES",
    "PURPOSES",
    "ConsentDecision",
    "ConsentError",
    "ConsentState",
    "Purpose",
    "consent_state",
    "is_permitted",
    "purpose",
    "purpose_keys",
    "record_decision",
    "withdrawal_effect",
]


class ConsentError(ValueError):
    """A consent operation that would misrepresent the lawful basis."""


class CapturedVia(str, enum.Enum):
    """How the decision was taken.

    Recorded because "did they actually see the notice" is the first question
    asked when a consent record is challenged, and "it was in the onboarding
    flow" is not an answer unless the flow is named.
    """

    ONBOARDING = "onboarding"
    SELF_SERVICE = "self_service"
    #: HR ticked it on somebody's behalf. Legitimate for a paper form that was
    #: signed; not legitimate as a bulk default, which is why it is a distinct
    #: value that a report can single out.
    HR_ON_BEHALF = "hr_on_behalf"
    CAREERS_PORTAL = "careers_portal"
    #: Migrated from a previous system. The weakest kind: we did not witness it.
    IMPORTED = "imported"


#: The capabilities a withdrawal can switch off. Named here rather than as free
#: strings so a purpose cannot claim to stop something that does not exist —
#: an invariant checks every `Purpose.stops` entry against this set.
CAPABILITIES: frozenset[str] = frozenset(
    {
        "culture_survey_invitations",
        "culture_sentiment_analysis",
        "ai_companion_queries",
        "ai_context_retrieval",
        "talent_pool_retention",
        "talent_pool_outreach",
        "ai_resume_screening",
        "background_check_initiation",
        "photo_in_directory",
        "birthday_announcements",
        "work_anniversary_announcements",
        "reference_contact",
    }
)

#: Who is asked about a purpose. An employee's My privacy screen lists only the
#: employee purposes; candidate purposes are asked and withdrawn on the careers
#: portal, where the candidate is, and stored with the candidate record.
AUDIENCES: frozenset[str] = frozenset({"employee", "candidate"})


@dataclass(frozen=True, slots=True)
class Purpose:
    """One thing we do with personal data, and why we are allowed to.

    `is_mandatory_for_employment` and `lawful_basis` are not independent. A
    purpose that is mandatory cannot rest on consent — if the person cannot say
    no, their agreement was not freely given, which is exactly what DPDP s.6(1)
    means by "unconditional". The invariant that enforces the pairing is worth
    more than this docstring.
    """

    key: str
    name: str
    #: Plain language, second person, no defined terms. This is the text a
    #: person reads before deciding, and a purpose described in the vocabulary
    #: of the privacy policy has not been described.
    description: str
    lawful_basis: LawfulBasis
    #: Category keys this purpose touches, from `categories.CATEGORIES`.
    data_categories: tuple[str, ...]
    is_mandatory_for_employment: bool = False
    #: What stops when consent is withdrawn. Empty for non-consent purposes;
    #: required to be non-empty for consent ones.
    stops: tuple[str, ...] = ()
    #: Whether this purpose sends data outside India. Cross-referenced against
    #: `transfers.SUB_PROCESSORS` by an invariant, so the two cannot disagree.
    sub_processors: tuple[str, ...] = ()
    #: Bumped when the description materially changes. A person who consented
    #: to v1 has not consented to v2, and the state machine says so rather than
    #: quietly carrying the old tick forward.
    version: int = 1
    #: "employee" or "candidate" — see `AUDIENCES`.
    audience: str = "employee"
    #: What a withdrawal deletes, in plain words. DPDP s.8(7)(a): data held only
    #: because of a consent is erased when that consent goes. The service does
    #: the deleting (`dpdp.service.withdraw_consent`); this is what the person
    #: is told before they confirm.
    withdrawal_erases: tuple[str, ...] = ()
    #: What a withdrawal does not delete, and why. The honest half.
    withdrawal_keeps: tuple[str, ...] = ()

    @property
    def requires_consent(self) -> bool:
        return self.lawful_basis is LawfulBasis.CONSENT

    @property
    def can_withdraw(self) -> bool:
        return self.requires_consent and not self.is_mandatory_for_employment

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "description": self.description,
            "lawful_basis": self.lawful_basis.value,
            "lawful_basis_label": self.lawful_basis.label,
            "data_categories": list(self.data_categories),
            "is_mandatory_for_employment": self.is_mandatory_for_employment,
            "requires_consent": self.requires_consent,
            "can_withdraw": self.can_withdraw,
            "stops_if_withdrawn": list(self.stops),
            "sub_processors": list(self.sub_processors),
            "version": self.version,
            "audience": self.audience,
            "withdrawal_erases": list(self.withdrawal_erases),
            "withdrawal_keeps": list(self.withdrawal_keeps),
        }


def _p(**kwargs: Any) -> Purpose:
    return Purpose(**kwargs)


#: Every purpose for which this product processes personal data.
#:
#: Read the `lawful_basis` column top to bottom: five of these are consent and
#: the rest are not, and that ratio is the point. **Counsel confirms the mapping
#: before go-live**; the mechanism does not depend on getting every one right,
#: only on each one being stated.
PURPOSES: dict[str, Purpose] = {
    p.key: p
    for p in (
        _p(
            key="employment_administration",
            name="Running your employment",
            description=(
                "Keeping your employee record, your reporting line, your "
                "designation and your documents, so your employer can administer "
                "your job. This is part of your employment; it is not something "
                "you are asked to agree to separately."
            ),
            lawful_basis=LawfulBasis.EMPLOYMENT_CONTRACT,
            data_categories=("identity", "organisational", "documents", "communication"),
            is_mandatory_for_employment=True,
        ),
        _p(
            key="payroll_processing",
            name="Calculating your pay",
            description=(
                "Working out what you are owed each month from your salary "
                "structure, your attendance and your leave, and producing your "
                "payslip."
            ),
            lawful_basis=LawfulBasis.EMPLOYMENT_CONTRACT,
            data_categories=("compensation", "attendance", "leave"),
            is_mandatory_for_employment=True,
        ),
        _p(
            key="payroll_disbursement",
            name="Paying you",
            description=(
                "Sending your net pay to your bank account, which means sharing "
                "your account number and IFSC with your employer's bank."
            ),
            lawful_basis=LawfulBasis.EMPLOYMENT_CONTRACT,
            data_categories=("financial", "compensation"),
            is_mandatory_for_employment=True,
        ),
        _p(
            key="statutory_filing",
            name="Statutory returns and deposits",
            description=(
                "Filing PF, ESI and TDS returns and depositing what is deducted "
                "from your pay. Your PAN, UAN and ESIC number go to the relevant "
                "authority. This is required by law and cannot be switched off — "
                "including after you leave."
            ),
            lawful_basis=LawfulBasis.LEGAL_OBLIGATION,
            data_categories=(
                "statutory_identifier",
                "compensation",
                "tax",
                "health",
                "dependent",
            ),
            is_mandatory_for_employment=True,
        ),
        _p(
            key="attendance_and_leave",
            name="Attendance and leave",
            description=(
                "Recording when you work and when you are away, including where "
                "you punched in from if your employer uses geofenced attendance."
            ),
            lawful_basis=LawfulBasis.EMPLOYMENT_CONTRACT,
            data_categories=("attendance", "leave"),
            is_mandatory_for_employment=True,
        ),
        _p(
            key="performance_management",
            name="Appraisals and goals",
            description=(
                "Running your appraisal cycle: goals, ratings, calibration and the "
                "feedback your manager and colleagues give."
            ),
            lawful_basis=LawfulBasis.EMPLOYMENT_CONTRACT,
            data_categories=("performance",),
            is_mandatory_for_employment=True,
        ),
        _p(
            key="employee_benefits",
            name="Benefits and insurance",
            description=(
                "Enrolling you and the family members you nominate in insurance "
                "and statutory benefits."
            ),
            lawful_basis=LawfulBasis.LEGAL_OBLIGATION,
            data_categories=("dependent", "health"),
            is_mandatory_for_employment=True,
        ),
        _p(
            key="security_and_fraud",
            name="Keeping the account secure",
            description=(
                "Recording sign-ins, devices and changes made in the system, so a "
                "compromised account can be spotted and what happened can be "
                "reconstructed afterwards."
            ),
            lawful_basis=LawfulBasis.LEGITIMATE_USE,
            data_categories=("account_security", "audit_trail"),
            is_mandatory_for_employment=True,
        ),
        _p(
            key="recruitment",
            name="Considering your application",
            description=(
                "Assessing you for the role you applied to: your CV, your "
                "interviews and the feedback from them."
            ),
            lawful_basis=LawfulBasis.LEGITIMATE_USE,
            data_categories=("recruitment", "identity"),
            audience="candidate",
        ),
        _p(
            key="dpdp_compliance",
            name="Honouring your privacy rights",
            description=(
                "Recording the choices you make about your data, the requests "
                "you send us, and what we did about them — so we can show, if "
                "asked, that we did what we said. This one cannot be switched "
                "off: it is the record of the switches."
            ),
            lawful_basis=LawfulBasis.LEGAL_OBLIGATION,
            data_categories=("governance_record", "audit_trail"),
            is_mandatory_for_employment=True,
        ),
        # ── From here down the answer can genuinely be no ────────────────────
        _p(
            key="culture_analytics",
            name="Culture and engagement surveys",
            description=(
                "Inviting you to pulse surveys and analysing the answers — "
                "including reading the free text for themes — to produce team "
                "and company culture measures. Your individual answers are only "
                "released as part of a group large enough to keep them anonymous."
            ),
            lawful_basis=LawfulBasis.CONSENT,
            data_categories=("culture_response",),
            stops=("culture_survey_invitations", "culture_sentiment_analysis"),
            sub_processors=("anthropic",),
            withdrawal_erases=("Survey invitations you have not answered yet",),
            withdrawal_keeps=(
                "Answers you already gave. They were stored without your name, so they "
                "cannot be traced back to you or picked out to delete; they count only "
                "in group results.",
            ),
        ),
        _p(
            key="ai_assistance",
            name="AI assistant",
            description=(
                "Answering your questions and drafting for you using an AI model. "
                "To do that, the parts of your record relevant to your question "
                "are sent to our AI provider, which processes them outside India. "
                "Declining means the assistant will not answer questions about you, "
                "and nothing about you is sent to an AI tool your HR team connects."
            ),
            lawful_basis=LawfulBasis.CONSENT,
            data_categories=("ai_interaction",),
            stops=("ai_companion_queries", "ai_context_retrieval"),
            sub_processors=("anthropic",),
            withdrawal_erases=(
                "Your Ask Mira conversations",
                "Feedback you gave on AI answers",
                "The questions and answers kept in the AI call log",
            ),
            withdrawal_keeps=(
                "A line for each past AI call (when, which feature, which model), with "
                "the text removed — the record your employer keeps of AI use.",
            ),
        ),
        _p(
            key="talent_pool",
            name="Keeping your application for future roles",
            description=(
                "Holding on to your application after this role is filled, so you "
                "can be considered for others. Separate from the role you applied "
                "to, and you can ask us to stop at any time."
            ),
            lawful_basis=LawfulBasis.CONSENT,
            data_categories=("recruitment",),
            stops=("talent_pool_retention", "talent_pool_outreach"),
            audience="candidate",
            withdrawal_erases=("Your place in the talent pool",),
        ),
        _p(
            key="ai_screening",
            name="AI help reading your résumé",
            description=(
                "Letting an AI model help the recruiter compare your résumé with the "
                "role. Your name, email and phone are removed first, and the model "
                "runs outside India. A person reads the result and makes every decision."
            ),
            lawful_basis=LawfulBasis.CONSENT,
            data_categories=("recruitment",),
            stops=("ai_resume_screening",),
            sub_processors=("anthropic",),
            audience="candidate",
            withdrawal_erases=("AI summaries of your résumé",),
        ),
        _p(
            key="background_verification",
            name="Background checks",
            description=(
                "Verifying your previous employment, your education and, where "
                "the role requires it, court records — through a verification "
                "agency. You will be told which agency before it starts."
            ),
            lawful_basis=LawfulBasis.CONSENT,
            data_categories=("background_check", "identity", "statutory_identifier"),
            stops=("background_check_initiation", "reference_contact"),
            audience="candidate",
            withdrawal_erases=("Checks that have not finished are cancelled",),
        ),
        _p(
            key="directory_visibility",
            name="Your photo in the company directory",
            description=(
                "Showing your photograph to colleagues in the directory, the org "
                "chart and search. Your name and role appear either way, and HR "
                "still sees the photo on your record."
            ),
            lawful_basis=LawfulBasis.CONSENT,
            data_categories=("identity",),
            stops=("photo_in_directory",),
            withdrawal_keeps=(
                "The photo itself stays on your HR record, where only you and HR see "
                "it. Remove it from your profile if you want it gone.",
            ),
        ),
        _p(
            key="celebrations",
            name="Birthday and work-anniversary announcements",
            description=(
                "Announcing your birthday (day and month, never the year) and your "
                "work anniversary to colleagues on the home page and in the "
                "celebrations calendar."
            ),
            lawful_basis=LawfulBasis.CONSENT,
            data_categories=("identity",),
            stops=("birthday_announcements", "work_anniversary_announcements"),
        ),
    )
}


def purpose(key: str) -> Purpose:
    try:
        return PURPOSES[key]
    except KeyError:
        raise KeyError(
            f"No processing purpose {key!r}. Known purposes: {', '.join(sorted(PURPOSES))}."
        ) from None


def purpose_keys() -> frozenset[str]:
    return frozenset(PURPOSES)


def consent_purposes(audience: str | None = None) -> tuple[Purpose, ...]:
    """The purposes a person is actually asked about.

    Everything else is disclosed in the notice and not offered as a choice,
    which is the honest presentation of a basis that is not consent.
    """
    return tuple(
        p
        for p in PURPOSES.values()
        if p.requires_consent and (audience is None or p.audience == audience)
    )


def employee_purpose_keys() -> tuple[str, ...]:
    """Every purpose an employee's own privacy screen shows: theirs, not a candidate's."""
    return tuple(key for key, p in PURPOSES.items() if p.audience == "employee")


@dataclass(frozen=True, slots=True)
class ConsentDecision:
    """One recorded answer. Insert-only — a withdrawal is a new row, not an edit.

    The version matters: a person who agreed to v1 of a purpose has not agreed
    to v2, and `consent_state` reports that as `stale` rather than as consent.
    """

    purpose_key: str
    granted: bool
    decided_at: datetime
    version: int
    captured_via: CapturedVia
    ip_address: str | None = None
    #: Which notice text they were shown, so "what did it say" is answerable.
    notice_id: str | None = None


def record_decision(
    purpose_key: str,
    *,
    granted: bool,
    now: datetime,
    captured_via: CapturedVia,
    ip_address: str | None = None,
    notice_id: str | None = None,
) -> ConsentDecision:
    """Build a consent decision, refusing where consent is not the basis.

    This is the guard the whole module exists for. A caller trying to record
    consent for payroll or statutory filing gets a `ConsentError` naming the
    actual basis, because the fix is not to force the record through — it is to
    stop asking.
    """
    spec = purpose(purpose_key)

    if not spec.requires_consent:
        raise ConsentError(
            f"{spec.name!r} does not rest on consent — its basis is "
            f"{spec.lawful_basis.value!r}. Recording a consent decision for it "
            "would tell the person they can withdraw something they cannot. "
            "Disclose it in the notice instead of asking about it."
        )
    if not granted and spec.is_mandatory_for_employment:
        # Unreachable while the pairing invariant holds; kept because the
        # invariant guards the catalogue and this guards the call.
        raise ConsentError(
            f"{spec.name!r} is marked mandatory for employment and cannot be "
            "declined, which means it should never have been modelled as consent."
        )

    return ConsentDecision(
        purpose_key=purpose_key,
        granted=granted,
        decided_at=now,
        version=spec.version,
        captured_via=captured_via,
        ip_address=ip_address,
        notice_id=notice_id,
    )


class ConsentStatus(str, enum.Enum):
    """Where one purpose stands for one person."""

    GRANTED = "granted"
    #: They said no, or they withdrew.
    DECLINED = "declined"
    #: Never asked. Distinct from declined — a blank is not a no, and a product
    #: that treats it as one has helped itself to an answer nobody gave.
    NOT_ASKED = "not_asked"
    #: They agreed to an earlier version of the purpose. Treated as no consent
    #: for the current version, and surfaced so they can be re-asked.
    STALE = "stale"
    #: Not a consent purpose at all. Reported so the compliance surface can
    #: show the person what happens on another basis, without a checkbox.
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True, slots=True)
class ConsentState:
    """One purpose, as it stands for one person, right now."""

    purpose_key: str
    purpose_name: str
    status: ConsentStatus
    lawful_basis: LawfulBasis
    can_withdraw: bool
    decided_at: datetime | None = None
    agreed_version: int | None = None
    current_version: int = 1
    stops_if_withdrawn: tuple[str, ...] = ()

    @property
    def is_permitted(self) -> bool:
        """Whether the dependent processing may run.

        A non-consent purpose is permitted — its basis is elsewhere. A consent
        purpose is permitted only on a current-version grant: NOT_ASKED and
        STALE both mean no, because neither is an answer.
        """
        if self.status is ConsentStatus.NOT_APPLICABLE:
            return True
        return self.status is ConsentStatus.GRANTED

    def as_dict(self) -> dict[str, Any]:
        return {
            "purpose": self.purpose_key,
            "name": self.purpose_name,
            "status": self.status.value,
            "lawful_basis": self.lawful_basis.value,
            "lawful_basis_label": self.lawful_basis.label,
            "can_withdraw": self.can_withdraw,
            "is_permitted": self.is_permitted,
            "decided_at": self.decided_at.isoformat() if self.decided_at else None,
            "agreed_version": self.agreed_version,
            "current_version": self.current_version,
            "stops_if_withdrawn": list(self.stops_if_withdrawn),
        }


def consent_state(
    decisions: list[ConsentDecision], *, purpose_keys_wanted: tuple[str, ...] = ()
) -> list[ConsentState]:
    """Fold an insert-only decision history into where each purpose stands.

    The latest decision per purpose wins, which is what makes a withdrawal a new
    row rather than an edit — the history stays intact and "when did they turn
    this off" remains answerable.
    """
    wanted = purpose_keys_wanted or tuple(PURPOSES)

    latest: dict[str, ConsentDecision] = {}
    for decision in sorted(decisions, key=lambda d: d.decided_at):
        latest[decision.purpose_key] = decision

    out: list[ConsentState] = []
    for key in wanted:
        spec = purpose(key)
        decision = latest.get(key)

        if not spec.requires_consent:
            status = ConsentStatus.NOT_APPLICABLE
        elif decision is None:
            status = ConsentStatus.NOT_ASKED
        elif not decision.granted:
            status = ConsentStatus.DECLINED
        elif decision.version < spec.version:
            status = ConsentStatus.STALE
        else:
            status = ConsentStatus.GRANTED

        out.append(
            ConsentState(
                purpose_key=key,
                purpose_name=spec.name,
                status=status,
                lawful_basis=spec.lawful_basis,
                can_withdraw=spec.can_withdraw,
                decided_at=decision.decided_at if decision else None,
                agreed_version=decision.version if decision else None,
                current_version=spec.version,
                stops_if_withdrawn=spec.stops,
            )
        )
    return out


def is_permitted(capability: str, states: list[ConsentState]) -> bool:
    """Whether one capability may run, given this person's consent state.

    The single question every consent-dependent feature asks. A capability
    nobody declared is refused rather than allowed: the failure mode of
    guessing wrong here is processing somebody's data without a basis, and the
    failure mode of refusing is a feature that visibly does not work.
    """
    if capability not in CAPABILITIES:
        raise ConsentError(
            f"Unknown capability {capability!r}. Add it to CAPABILITIES and name "
            "it on the purpose whose withdrawal switches it off — an unlisted "
            "capability is refused, because guessing here means processing "
            "somebody's data on a basis nobody wrote down."
        )

    by_key = {state.purpose_key: state for state in states}
    for spec in PURPOSES.values():
        if capability not in spec.stops:
            continue
        state = by_key.get(spec.key)
        if state is None or not state.is_permitted:
            return False
    return True


@dataclass(frozen=True, slots=True)
class WithdrawalEffect:
    """What a person is told will happen before they confirm a withdrawal.

    Shown at the point of withdrawal, not afterwards. "Your survey history stays
    but no new invitations go out" is the kind of thing somebody wants to know
    before they click, and finding out after is how a withdrawal turns into a
    grievance.
    """

    purpose_key: str
    purpose_name: str
    stops: tuple[str, ...]
    #: What does not stop, and why. The honest half.
    retained: tuple[str, ...]
    summary: str
    #: What the withdrawal deletes.
    erased: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "purpose": self.purpose_key,
            "name": self.purpose_name,
            "stops": list(self.stops),
            "retained": list(self.retained),
            "erased": list(self.erased),
            "summary": self.summary,
        }


def withdrawal_effect(purpose_key: str) -> WithdrawalEffect:
    """What withdrawing this consent does and does not do."""
    spec = purpose(purpose_key)

    if not spec.can_withdraw:
        raise ConsentError(
            f"{spec.name!r} cannot be withdrawn — its basis is "
            f"{spec.lawful_basis.value!r}, not consent. Offering a withdrawal "
            "button here would promise something we cannot do."
        )

    # DPDP s.8(7)(a): what was held only because of this consent is erased when
    # it goes. What was held on another basis too — the photo on an HR record,
    # an AI call's log line — is named, with why it stays.
    erased = spec.withdrawal_erases
    retained = spec.withdrawal_keeps
    summary = f"{spec.name} has stopped." + (
        " What was held only for it has been deleted." if erased else ""
    )
    return WithdrawalEffect(
        purpose_key=spec.key,
        purpose_name=spec.name,
        stops=spec.stops,
        retained=retained,
        summary=summary,
        erased=erased,
    )
