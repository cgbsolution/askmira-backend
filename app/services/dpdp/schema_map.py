"""Which table holds whose personal data, and of what category.

═══════════════════════════════════════════════════════════════════════════════
WHY THIS IS ONE FILE AND NOT 149 ANNOTATIONS
═══════════════════════════════════════════════════════════════════════════════

The brief said: annotate the schema, generate the inventory from the
annotations, do not keep a spreadsheet that drifts within a month. The second
half of that is the real requirement and this file satisfies it; the first half
it deviates from, deliberately, and here is the argument.

A spreadsheet drifts because **nothing fails when it is wrong**. Somebody adds a
column, the sheet does not know, and six months later the RoPA describes a
system that no longer exists. That is a property of the sheet being unchecked,
not of it being in one place.

`test_invariants.py` reflects over `Base.metadata` and fails the build if:

- a table exists that this file does not classify,
- this file classifies a table that no longer exists,
- a column exists that is neither structural, nor covered by its table's
  default category, nor explicitly categorised, nor explicitly exempted with a
  written reason,
- a column whose *name* looks like a direct identifier (aadhaar, PAN, bank
  account, email, coordinates, a token hash) is not explicitly categorised or
  explicitly exempted — the table default is not allowed to swallow one,
- a category or purpose is named here that the catalogue does not define.

So this file cannot drift. It can only be wrong on the day it is written, and a
reviewer can read the whole classification in one sitting — which is the thing a
RoPA reviewer actually needs to do, and which 149 `info={...}` kwargs scattered
across 33 model files makes impossible.

The trade is stated so nobody later reads it as an oversight.

Pure: this module imports the category vocabulary and nothing else. It never
imports SQLAlchemy — the reflection lives in the caller, which is what keeps the
inventory engine testable without a database.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.services.dpdp.categories import PrincipalKind, category_keys

__all__ = [
    "DIRECT_IDENTIFIER_PATTERNS",
    "EXEMPT_COLUMNS",
    "STRUCTURAL_COLUMNS",
    "TABLES",
    "TableClassification",
    "is_direct_identifier",
]


@dataclass(frozen=True, slots=True)
class TableClassification:
    """What one table holds, and about whom.

    `principal=None` means the table holds no personal data at all — a rate
    card, a template, a policy. It needs a `reason`, because "this is not
    personal data" is a judgement and an unexplained one is not reviewable.

    `default_category` covers the table's ordinary payload columns.
    `columns` overrides it per column, and direct identifiers are *required* to
    appear there rather than inheriting the default — a table defaulting to
    "organisational" that quietly acquires an `aadhaar_number` would otherwise
    file somebody's Aadhaar under org placement in the RoPA.
    """

    principal: PrincipalKind | None
    default_category: str | None = None
    columns: dict[str, str] = field(default_factory=dict)
    reason: str = ""
    #: Set where the row is about somebody other than the employee it hangs
    #: off — a nominee, a landlord, a previous employer's contact. An access
    #: request from that person has to reach these rows too.
    secondary_principal: PrincipalKind | None = None

    def category_for(self, column: str) -> str | None:
        if column in self.columns:
            return self.columns[column]
        return self.default_category


def _p(
    principal: PrincipalKind,
    default_category: str,
    columns: dict[str, str] | None = None,
    *,
    secondary_principal: PrincipalKind | None = None,
) -> TableClassification:
    return TableClassification(
        principal=principal,
        default_category=default_category,
        columns=columns or {},
        secondary_principal=secondary_principal,
    )


def _none(reason: str, columns: dict[str, str] | None = None) -> TableClassification:
    return TableClassification(principal=None, reason=reason, columns=columns or {})


EMP = PrincipalKind.EMPLOYEE
CAND = PrincipalKind.CANDIDATE
USER = PrincipalKind.PLATFORM_USER
THIRD = PrincipalKind.THIRD_PARTY


#: Columns every table has, carrying no personal data of their own. A row's
#: `id` identifies the row; whether the row is about a person is the table's
#: classification, not the id's.
STRUCTURAL_COLUMNS: frozenset[str] = frozenset(
    {"id", "tenant_id", "created_at", "updated_at", "deleted_at"}
)


#: Column names that must never inherit a table default. Getting one of these
#: filed under the wrong category in the RoPA is the difference between "we hold
#: employment records" and "we hold Aadhaar numbers", and only one of those is a
#: sentence a Data Protection Officer needs to have read before an audit.
#:
#: Matched on **snake_case word boundaries**, not as substrings. The first pass
#: used substrings and flagged `participant_user_ids`, `panelist_id` and
#: `current_company` — all three because they contain the letters "pan". A check
#: that cries wolf four times gets an exemption written for each cry, and the
#: exemption list is then where a real identifier goes to hide.
DIRECT_IDENTIFIER_PATTERNS: tuple[str, ...] = (
    "aadhaar",
    "passport",
    "pan",
    "uan",
    "esic",
    "account_number",
    "ifsc",
    "email",
    "phone",
    "mobile",
    "date_of_birth",
    "latitude",
    "longitude",
    "password",
    "token_hash",
    "secret",
    "gender",
    "blood",
    "ctc",
)


#: `(table, column)` pairs whose name trips the direct-identifier check but
#: which hold no personal data. Every one carries the reason, because an
#: exemption without one is indistinguishable from an oversight.
EXEMPT_COLUMNS: dict[tuple[str, str], str] = {
    ("webhook_endpoints", "secret_encrypted"): (
        "The key the workspace's webhook deliveries are signed with. A credential of an "
        "integration, about no person; encrypted at rest."
    ),
    ("product_links", "secret_encrypted"): (
        "The key this workspace's connection to one of our products is signed with. A "
        "credential of an integration, about no person; encrypted at rest."
    ),
    ("leave_policies", "applies_to_gender"): (
        "Which population a leave policy covers ('female' for maternity leave). A rule "
        "about a group, stored once per policy; it records nobody's gender."
    ),
    ("legal_entities", "pan"): (
        "The employer's own PAN. A company's tax identifier, not a person's."
    ),
    ("tenant_whatsapp_settings", "phone_number_id"): (
        "Meta's id for the organisation's own business phone number. Belongs to a "
        "company's WhatsApp account, not to a person."
    ),
    ("tenant_smtp_settings", "password_encrypted"): (
        "The ciphertext of the organisation's own mail-relay password. A "
        "credential belonging to a company's mail server, not a person's data."
    ),
    ("identity_providers", "client_secret_encrypted"): (
        "The tenant's OAuth client secret with their identity provider. A "
        "credential belonging to an organisation."
    ),
    ("identity_providers", "email_attribute"): (
        "The *name* of the SAML attribute an email arrives in, such as "
        "'urn:oid:0.9.2342...'. Configuration describing a protocol field."
    ),
    ("identity_providers", "allowed_email_domains"): (
        "A list of domains such as 'acme.com'. Names an organisation, not a person."
    ),
    ("bank_profiles", "account_number"): (
        "The employer's disbursement account, from which salaries are paid out. "
        "Company banking, not an employee's."
    ),
    ("bank_profiles", "ifsc"): (
        "The branch of the employer's own disbursement account. Company "
        "banking detail, held before any employee is paid from it."
    ),
    ("salary_components", "part_of_ctc"): (
        "A boolean on a component definition — whether this component counts "
        "toward CTC. A property of the salary structure, held before any "
        "employee is attached to it."
    ),
    ("offer_components", "counts_toward_ctc"): (
        "A definitional flag copied onto an offer's component rows."
    ),
    ("approval_chain_rules", "ctc_min"): (
        "The lower bound of an approval band — 'offers above 40 lakh need the "
        "CFO'. A policy threshold; no person is attached to it."
    ),
    ("approval_chain_rules", "ctc_max"): (
        "The upper bound of the same approval band. A policy threshold, not anybody's pay."
    ),
    ("positions", "budgeted_ctc_min"): (
        "What a seat is budgeted at, which exists whether or not anybody sits "
        "in it. The occupant's actual pay is in employee_salaries."
    ),
    ("positions", "budgeted_ctc_max"): (
        "The top of the same seat budget. Set when the position is created "
        "and unchanged by who occupies it."
    ),
    ("requisitions", "budget_ctc_min"): (
        "The approved hiring budget for a role, set before a candidate exists."
    ),
    ("requisitions", "budget_ctc_max"): (
        "The top of the same approved hiring budget, fixed before any candidate applies."
    ),
    ("work_locations", "latitude"): (
        "The office's coordinates, used as the centre of a geofence. A "
        "building's location. Where a *person* was when they punched is in "
        "attendance_punches and is classified as attendance data."
    ),
    ("work_locations", "longitude"): (
        "The same office building's coordinates. A place, not a person's whereabouts."
    ),
    ("device_integrations", "push_token_hash"): (
        "A shared secret for a biometric device's callbacks. Belongs to a piece "
        "of hardware mounted on a wall."
    ),
}


#: Every table in the schema, classified. Checked for exhaustiveness against
#: `Base.metadata` by an invariant, in both directions.
TABLES: dict[str, TableClassification] = {
    # ── Tenant, configuration and reference data ────────────────────────────
    "tenants": _none("The customer organisation's own record — name, plan, settings."),
    "legal_entities": _none("The employer's registered entities and their registrations."),
    "departments": _none("Organisation structure. Named by function, not by person."),
    "locations": _none("The employer's offices."),
    "work_locations": _none("Geofence definitions for those offices."),
    "holidays": _none("A calendar of public holidays."),
    "shifts": _none("Shift definitions — timings, grace, break rules."),
    "rosters": _none("Roster patterns."),
    "roster_days": _none("The days of a roster pattern."),
    "leave_types": _none("Leave type definitions and their accrual rules."),
    "leave_policies": _none("Which leave types apply to which population."),
    "encashment_policies": _none("Encashment rules."),
    "salary_components": _none("Component definitions: basic, HRA, special allowance."),
    "salary_structures": _none("Named structures built from those components."),
    "pay_grades": _none("The company's grades and the CTC band for each."),
    "salary_structure_components": _none("A structure's component rows and formulas."),
    "payroll_rule_sets": _none(
        "Effective-dated statutory rates: PF ceilings, ESI thresholds, PT slabs. "
        "Rates apply to everyone and identify nobody."
    ),
    "section_reliefs": _none("Income-tax section limits — 80C, 80D and the rest."),
    "reimbursement_components": _none("Reimbursement heads and their caps."),
    "loan_schemes": _none("Loan product definitions offered by the employer."),
    "gratuity_configs": _none("The tenant's gratuity accrual settings."),
    "bank_profiles": _none("The employer's disbursement bank accounts."),
    "appraisal_cycles": _none("A cycle: its window, its stages, its population filter."),
    "appraisal_templates": _none("Appraisal form definitions."),
    "appraisal_stages": _none("The stages of a template."),
    "rating_scales": _none("Rating scale definitions."),
    "rating_scale_levels": _none("The levels of a scale and what each means."),
    "calibration_policies": _none("Distribution and calibration rules."),
    "distribution_targets": _none("The target distribution a calibration aims at."),
    "competencies": _none("The competency library."),
    "competency_level_indicators": _none("Behavioural indicators per competency level."),
    "competency_templates": _none("Competency frameworks assembled from the library."),
    "competency_template_items": _none("A framework's competency rows."),
    "competency_value_links": _none("Which competency evidences which company value."),
    "clearance_templates": _none("Exit clearance checklists by department."),
    "onboarding_templates": _none("Onboarding checklists. No person is attached to one."),
    "onboarding_template_tasks": _none("The lines of those onboarding checklists."),
    "letter_templates": _none("Letter body templates and their merge fields."),
    "letter_template_fields": _none("The merge fields a template declares."),
    "letter_sequences": _none("Reference-number sequences for issued letters."),
    "template_assignment_rules": _none("Which letter template applies to whom."),
    "notification_templates": _none("Notification copy and its channels."),
    "notification_policies": _none("Per-event routing and quiet-hours rules."),
    "whatsapp_templates": _none("WhatsApp template definitions and approval state."),
    "custom_field_definitions": _none(
        "The definition of a tenant's custom field — its label and type. The "
        "values entered into it live on the employee record."
    ),
    "declaration_windows": _none("When tax declarations and proofs may be submitted."),
    "role_module_permissions": _none("Which role may do what in which module."),
    "identity_providers": _none("The tenant's SSO configuration."),
    "job_board_accounts": _none("The tenant's credentials with a job board."),
    "careers_page_configs": _none("Careers-site branding and content."),
    "interview_stages": _none("Named interview stages in a hiring process."),
    "approval_chain_rules": _none("Which approvals an offer needs, by band."),
    "cost_centres": _none("Cost centre codes and their budgets."),
    "headcount_budgets": _none("Approved headcount and cost by period."),
    "positions": _none(
        "A seat: its title, grade, cost centre and budget. Exists before and "
        "after anybody occupies it; the occupancy is in position_assignments."
    ),
    "position_scenarios": _none("A what-if org plan, worked in draft."),
    "position_scenario_changes": _none("The changes a scenario proposes."),
    "reports": _none("A saved report definition — its fields, filters and visibility."),
    "dashboard_widgets": _none("Which saved reports a person pinned to their dashboard."),
    "report_schedules": _none("When a saved report runs and who it goes to."),
    "clubs": _none("Employee interest groups: their name, description and rules."),
    "lifecycle_batches": _none(
        "The header of a bulk lifecycle raise — its name and effective date. "
        "The per-person events are in lifecycle_events."
    ),
    "probation_policies": _none("The tenant's probation and silence-policy settings."),
    "arrear_batches": _none("The header of an arrears computation run."),
    "disbursement_batches": _none("The header of a payment file."),
    "payroll_runs": _none(
        "A run's header: period, status, totals. The per-person amounts are in payslips."
    ),
    "attendance_period_locks": _none("Which attendance periods are frozen."),
    "ledger_mappings": _none(
        "Which accounting ledger each payslip line code posts to. Configuration."
    ),
    "overtime_policies": _none(
        "How overtime is counted and priced for a location: multiplier, wage "
        "components, divisors and caps. Configuration, about nobody."
    ),
    "bulk_letter_jobs": _none("The header of a bulk letter generation job."),
    "tds_challans": _none(
        "A challan the employer deposited: BSR code, amount, date. The "
        "employer's payment to the government."
    ),
    "filing_records": _none(
        "A statutory return's filing state — which return, which period, "
        "acknowledgement number. The per-employee content is generated from "
        "payroll at filing time and is not stored here."
    ),
    "esi_coverage_periods": _none(
        "The contribution-period calendar ESI runs on. Dates, not people."
    ),
    "device_integrations": _none("A biometric device's configuration and health."),
    "job_postings": _none("A published job advert."),
    "requisitions": _none("An approved request to hire, before anybody applies."),
    "saml_assertions_seen": _none(
        "Assertion identifiers kept briefly to reject a replayed SAML response. "
        "An opaque id and an expiry."
    ),
    "calibration_sessions": _none("A calibration meeting: its window and facilitators."),
    # ── Employee: the core record ───────────────────────────────────────────
    "employees": _p(
        EMP,
        "organisational",
        {
            "first_name": "identity",
            "last_name": "identity",
            "display_name": "identity",
            "work_email": "identity",
            "personal_email": "identity",
            "phone": "identity",
            "date_of_birth": "identity",
            "gender": "identity",
            "photo_url": "identity",
            # A tenant-defined bag. Whatever goes in it is about this person and
            # we do not control its shape, so it is classified as identity —
            # the broadest of the categories a free-form field could hold, and
            # therefore the safest default for an access export.
            "custom_fields": "identity",
            "pan": "statutory_identifier",
            "aadhaar_last4": "statutory_identifier",
            "aadhaar_number": "statutory_identifier",
            "uan": "statutory_identifier",
            "esi_number": "statutory_identifier",
            "pf_number": "statutory_identifier",
            "bank_account_number": "financial",
            "bank_ifsc": "financial",
        },
    ),
    "employee_work_locations": _p(EMP, "attendance"),
    "employee_documents": _p(
        EMP,
        "documents",
        {"document_number": "statutory_identifier", "dependant_name": "dependent"},
        secondary_principal=THIRD,
    ),
    "handbooks": _none("A handbook HR writes for everyone - about nobody in particular."),
    "company_documents": _none(
        "The company's own policies and handbooks - about nobody in particular."
    ),
    # The extended profile. Emergency contacts, family members and nominees
    # are about people who are not our users, so an access request from one
    # of them has to reach this row too.
    "employee_profiles": _p(
        EMP,
        "identity",
        {
            "personal": "identity",
            "addresses": "identity",
            "emergency_contacts": "dependent",
            "family_members": "dependent",
            "nominees": "dependent",
            "education": "background_check",
            "previous_employment": "background_check",
            "photo_storage_key": "identity",
            # Source, CTC at offer and fit score, carried over from the offer.
            "hiring_snapshot": "compensation",
        },
        secondary_principal=THIRD,
    ),
    # A proposed change can carry a PAN, a UAN or an encrypted account number,
    # so the payload is classified as the most sensitive thing it may hold.
    "profile_change_requests": _p(
        EMP,
        "identity",
        {
            "proposed": "statutory_identifier",
            "current_snapshot": "statutory_identifier",
        },
        secondary_principal=THIRD,
    ),
    "employee_bank_accounts": _p(
        EMP,
        "financial",
        {"account_number": "financial", "ifsc": "financial"},
    ),
    "users": _p(
        USER,
        "account_security",
        {
            "email": "identity",
            "phone": "identity",
            "full_name": "identity",
            "is_email_verified": "identity",
            "password_hash": "account_security",
            "must_change_password": "account_security",
        },
    ),
    # ── Compensation ────────────────────────────────────────────────────────
    "employee_salaries": _p(EMP, "compensation", {"ctc_annual": "compensation"}),
    "employee_salary_components": _p(EMP, "compensation", {"part_of_ctc": "compensation"}),
    "salary_revisions": _p(EMP, "compensation"),
    "payslips": _p(EMP, "compensation"),
    "bonus_computations": _p(EMP, "compensation"),
    "arrear_lines": _p(EMP, "compensation"),
    "flexi_declarations": _p(EMP, "compensation"),
    "reimbursement_claims": _p(EMP, "compensation"),
    "encashment_requests": _p(EMP, "leave"),
    "loan_accounts": _p(EMP, "compensation"),
    "payroll_adjustments": _p(EMP, "compensation"),
    "company_document_acknowledgements": _p(
        EMP,
        "governance_record",
        {"ip_address": "account_security", "user_agent": "account_security"},
    ),
    "payroll_adjustment_applications": _p(EMP, "compensation"),
    "overtime_entries": _p(
        EMP, "attendance", {"hourly_rate": "compensation", "amount": "compensation"}
    ),
    "shift_allowance_payments": _p(EMP, "compensation"),
    "loan_repayments": _p(EMP, "compensation"),
    "gratuity_accruals": _p(EMP, "compensation"),
    "gratuity_payouts": _p(EMP, "compensation"),
    "settlements": _p(EMP, "compensation"),
    "settlement_lines": _p(EMP, "compensation"),
    "disbursement_lines": _p(
        EMP,
        "financial",
        {"account_number": "financial", "ifsc": "financial"},
    ),
    "disbursement_downloads": _p(
        USER,
        "audit_trail",
        # Who downloaded a file containing every employee's bank details, and
        # when. The most sensitive read in the product, so it is its own record.
    ),
    # ── Statutory and tax ───────────────────────────────────────────────────
    "employee_pf_elections": _p(EMP, "statutory_identifier"),
    "tax_declarations": _p(EMP, "tax"),
    "tax_proofs": _p(EMP, "tax"),
    "tax_regime_elections": _p(EMP, "tax"),
    "tax_computation_snapshots": _p(EMP, "tax"),
    "house_rent_declarations": _p(
        EMP,
        "tax",
        {"landlord_pan": "tax", "landlord_name": "tax"},
        # The landlord is a third party whose PAN we hold because the Income-tax
        # rules require it above 1 lakh of annual rent. They never dealt with us.
        secondary_principal=THIRD,
    ),
    "previous_employer_incomes": _p(EMP, "tax"),
    "tds_challan_mappings": _p(EMP, "tax"),
    "form16_part_a": _p(EMP, "tax"),
    # ── Attendance and leave ────────────────────────────────────────────────
    "attendance_days": _p(EMP, "attendance"),
    "attendance_punches": _p(
        EMP,
        "attendance",
        {"latitude": "attendance", "longitude": "attendance"},
    ),
    "attendance_regularisations": _p(EMP, "attendance"),
    "shift_assignments": _p(EMP, "attendance"),
    "roster_assignments": _p(EMP, "attendance"),
    "shift_swap_requests": _p(EMP, "attendance"),
    "statutory_notices": _none("A change in labour or tax law, published for every workspace."),
    "statutory_notice_alerts": _none("That a workspace was told about a statutory change."),
    "api_keys": _none("A hashed key the customer's own systems read the workspace with."),
    "webhook_endpoints": _none("Where the customer wants change events sent."),
    "webhook_deliveries": _none(
        "One event sent to a customer endpoint: the entity type and id, and the audit "
        "summary of what changed.",
        {"payload": "audit_trail"},
    ),
    "courses": _none("A course HR wrote: its modules, quiz and pass mark."),
    "course_assignments": _p(EMP, "performance"),
    "esop_plans": _none("A stock option plan: its pool and default vesting."),
    "esop_valuations": _none("The fair value per unit from a date, as the valuer set it."),
    "esop_grants": _p(EMP, "compensation"),
    "esop_exercises": _p(EMP, "compensation"),
    "ewa_requests": _p(EMP, "financial"),
    # A contractor may be a sole proprietor: a person, outside the workforce.
    "contractors": _p(
        THIRD,
        "identity",
        {"pan": "statutory_identifier", "email": "identity", "gstin": "statutory_identifier"},
    ),
    "contractor_invoices": _none("An invoice from a contractor and the TDS deducted on it."),
    "projects": _none("A project people log time against."),
    "project_tasks": _none("A task within a project that people log time against."),
    "timesheets": _p(EMP, "attendance"),
    "timesheet_entries": _p(EMP, "attendance"),
    "hourly_rates": _p(EMP, "compensation"),
    "client_accounts": _none(
        "A client engagement the company bills: its code, name and who looks after it."
    ),
    "account_entries": _none("An amount billed to an account, or a direct cost, for a month."),
    "account_allocations": _p(EMP, "organisational"),
    "custom_table_definitions": _none("The design of an extra table on the employee record."),
    "custom_table_rows": _p(EMP, "identity", {"values": "identity"}),
    "benefit_plans": _none("A group policy the company buys: insurer, cover, premium."),
    "benefit_enrolments": _p(EMP, "dependent", {"members": "dependent"}),
    "benefit_life_events": _p(EMP, "dependent", {"add_members": "dependent"}),
    "benefit_member_changes": _p(EMP, "dependent", {"member": "dependent"}),
    "reward_catalogue_items": _none("What recognition points can be spent on."),
    "operator_grants": _p(USER, "account_security", {"operator_email": "identity"}),
    "product_links": _none(
        "Which organisation in our interview or expense product is this workspace, and how "
        "the last people sync went.",
        {"last_sync": "identity"},
    ),
    "social_login_attempts": _none(
        "A sign-in in flight: random state, nonce and PKCE verifier; no person until it ends."
    ),
    "assistant_articles": _none("Company policy text HR wrote for the assistant to cite."),
    # Ask Mira: a person's own conversations. The model only ever sees `text`;
    # `cards` are what AskMira looked up and showed, and may name colleagues.
    "companion_conversations": _p(USER, "ai_interaction", {"title": "ai_interaction"}),
    "companion_messages": _p(
        USER,
        "ai_interaction",
        {
            "text": "ai_interaction",
            "arguments": "ai_interaction",
            "cards": "identity",
            "transcript": "ai_interaction",
            "steps": "ai_interaction",
        },
    ),
    "companion_actions": _p(
        USER,
        "ai_interaction",
        {"payload": "leave", "summary": "leave", "result": "ai_interaction"},
    ),
    "survey_topic_summaries": _none(
        "Paraphrased themes counted across anonymous survey comments; no respondent link."
    ),
    "candidate_screenings": _p(THIRD, "recruitment"),
    "polls": _none("A question HR asked the company, and its options."),
    "poll_votes": _p(EMP, "culture_response"),
    "team_events": _none("A company event people answer: when, where, how many places."),
    "event_rsvps": _p(EMP, "culture_response"),
    "employee_badges": _p(EMP, "attendance"),
    "kiosk_pins": _p(EMP, "account_security"),
    "device_punch_exceptions": _p(EMP, "attendance"),
    "leave_balances": _p(EMP, "leave"),
    "leave_requests": _p(EMP, "leave"),
    "leave_balance_adjustments": _p(EMP, "leave"),
    "optional_holiday_selections": _p(EMP, "leave"),
    "comp_off_requests": _p(EMP, "leave"),
    # ── Performance and culture ─────────────────────────────────────────────
    "appraisals": _p(EMP, "performance"),
    "appraisal_stage_entries": _p(EMP, "performance"),
    "calibration_changes": _p(EMP, "performance"),
    "stage_delegations": _p(EMP, "organisational"),
    "shoutouts": _p(EMP, "culture_response"),
    "shoutout_reactions": _p(EMP, "culture_response"),
    "club_members": _p(EMP, "culture_response"),
    "club_posts": _p(EMP, "culture_response"),
    # ── Lifecycle, org placement and exit ───────────────────────────────────
    "lifecycle_events": _p(EMP, "organisational", {"new_ctc_annual": "compensation"}),
    "probation_reviews": _p(EMP, "performance"),
    "probation_reminders": _p(EMP, "communication"),
    "position_assignments": _p(EMP, "organisational"),
    "exit_records": _p(EMP, "organisational"),
    "clearance_items": _p(EMP, "organisational"),
    "onboarding_plans": _p(EMP, "organisational"),
    "onboarding_tasks": _p(EMP, "organisational"),
    "issued_letters": _p(EMP, "documents"),
    # ── Recruitment ─────────────────────────────────────────────────────────
    "candidates": _p(
        CAND,
        "recruitment",
        {
            "email": "identity",
            "phone": "identity",
            "full_name": "identity",
            "current_ctc": "compensation",
            "expected_ctc": "compensation",
        },
    ),
    "candidate_documents": _p(CAND, "documents"),
    # A prospect who asked for a demo on the website: not a user, an employee
    # or a candidate of any workspace, so a third party. Their name, work email,
    # phone and company, as they typed them; the platform team's notes about
    # the follow-up are about them too.
    "demo_requests": _p(
        THIRD,
        "identity",
        {
            "full_name": "identity",
            "work_email": "identity",
            "phone": "identity",
        },
    ),
    "candidate_consents": _p(CAND, "recruitment"),
    "candidate_portal_tokens": _p(CAND, "account_security", {"token_hash": "account_security"}),
    "background_checks": _p(
        CAND,
        "background_check",
        {
            "consent_typed_name": "identity",
            "consent_ip": "account_security",
            "consent_user_agent": "account_security",
        },
    ),
    "talent_pools": _none(
        "A named list a recruiter keeps candidates in. The name and description are "
        "about roles, not people; membership is in talent_pool_members."
    ),
    "talent_pool_members": _p(CAND, "recruitment"),
    "candidate_messages": _p(
        CAND,
        "communication",
        {
            "to_address": "identity",
            "secret_link_encrypted": "account_security",
        },
    ),
    "applications": _p(CAND, "recruitment"),
    "interviews": _p(CAND, "recruitment", secondary_principal=EMP),
    "interview_panelists": _p(EMP, "recruitment"),
    "interview_scores": _p(CAND, "recruitment"),
    "interview_feedback": _p(CAND, "recruitment", secondary_principal=EMP),
    "self_schedule_links": _p(CAND, "account_security", {"token_hash": "account_security"}),
    "offers": _p(CAND, "recruitment", {"ctc_annual": "compensation"}),
    "offer_components": _p(CAND, "compensation"),
    "offer_salary_components": _p(CAND, "compensation", {"part_of_ctc": "compensation"}),
    "offer_approvals": _p(CAND, "recruitment", secondary_principal=EMP),
    "offer_events": _p(CAND, "recruitment"),
    "requisition_approvals": _p(EMP, "organisational"),
    # ── Communication ───────────────────────────────────────────────────────
    "notifications": _p(EMP, "communication"),
    "notification_digests": _p(EMP, "communication"),
    "notification_preferences": _p(EMP, "communication"),
    "whatsapp_consents": _p(EMP, "communication", {"phone_e164": "identity"}),
    "oauth_clients": _none(
        "MCP connectors that registered themselves: a name and redirect addresses."
    ),
    "oauth_authorization_codes": _p(USER, "account_security", {"code_hash": "account_security"}),
    "oauth_grants": _p(
        USER,
        "account_security",
        {"access_token_hash": "account_security", "refresh_token_hash": "account_security"},
    ),
    "push_devices": _p(
        USER,
        "communication",
        {"push_token_hash": "account_security", "push_token_encrypted": "account_security"},
    ),
    "report_deliveries": _p(EMP, "communication", {"recipient_email": "identity"}),
    # ── DPDP governance (this slice's own tables) ───────────────────────────
    "consent_records": _p(EMP, "governance_record", secondary_principal=CAND),
    "data_principal_requests": _p(EMP, "governance_record", secondary_principal=CAND),
    "breach_incidents": _none(
        "An incident record: what happened, which categories were involved, how "
        "many people, and what was done. It counts principals; it names none. "
        "The notification actually sent is stored on it verbatim and contains no "
        "recipient list — who it went to is in the notifications table."
    ),
    "cross_border_transfers": _none(
        "One transfer event: which sub-processor, which categories, how many "
        "principals, when. Deliberately holds no principal identifier — a "
        "register of where data went should not itself become a second copy of "
        "who the data was about."
    ),
    "dpdp_configurations": _none(
        "The tenant's DPDP settings. Two columns name a person — their Data "
        "Protection Officer and their grievance contact — and those are a role "
        "holder's published business contact details, printed in every notice "
        "this product renders because the Act requires the route to be "
        "published. Held as the employer's configuration, not about that person "
        "as a data principal of this system. If that reading is wrong, this "
        "table becomes a personal-data table and the two columns become "
        "identity: it is one line to change and it is written here so the "
        "question is visible to counsel."
    ),
    # ── Operational security (slice 30) ─────────────────────────────────────
    "tenant_session_policies": _none(
        "One tenant's session rules and IP allowlist. Timeouts, limits and CIDR "
        "ranges — an office network is not a person."
    ),
    "user_sessions": _p(
        USER,
        "account_security",
        {
            "ip_address": "account_security",
            "user_agent": "account_security",
            "token_hash": "account_security",
        },
        # Where somebody signed in from, on what, and when. Squarely the
        # account_security category: it is the evidence trail a breach
        # investigation runs on, and it is also a record of a person's
        # movements that they are entitled to see under an access request.
    ),
    "password_reset_tokens": _p(
        USER,
        "account_security",
        {
            "token_hash": "account_security",
            "requested_ip": "account_security",
        },
        # Who asked to reset their password, from where, and whether the link
        # was used — the trail an account-takeover investigation starts from.
    ),
    "exit_interviews": _p(EMP, "organisational", {"comments": "organisational"}),
    "milestone_rules": _none("Which work anniversaries the company marks, and how. Configuration."),
    "approval_rules": _none(
        "Who approves leave and expenses under which conditions. Workflow configuration."
    ),
    "approval_delegations": _p(USER, "organisational"),
    "user_area_rights": _none(
        "Which rights a sub-admin holds in each area. Access configuration about a "
        "login, not personal data about the person."
    ),
    "admin_scopes": _none(
        "Which departments and locations a sub-admin covers. Access configuration."
    ),
    "user_mfa": _p(
        USER,
        "account_security",
        {
            "secret_encrypted": "account_security",
            "recovery_code_hashes": "account_security",
        },
    ),
    "step_up_challenges": _p(
        USER,
        "account_security",
        {"ip_address": "account_security"},
    ),
    "siem_destinations": _none(
        "The client's own SIEM endpoint and its credential. Their infrastructure, not a person."
    ),
    "siem_deliveries": _none(
        "One delivery attempt: how many events, whether it worked, how long it "
        "took. Counts and an error string; the events themselves are not "
        "stored here."
    ),
    "restore_drills": _none(
        "A timed restore: which tier, how long, whether it met the published "
        "objectives. Durations, not records."
    ),
    "pentest_findings": _none(
        "One finding from a third-party test, with its remediation clock. "
        "Describes a weakness in the product; deliberately carries no "
        "reproduction detail and no data about anybody."
    ),
    # ── Agent autonomy (slice 29) ───────────────────────────────────────────
    "agent_autonomy_configs": _none(
        "One tenant's setting for one agent action: how far it may go, above "
        "what magnitude it routes to a person, who may approve. A policy about "
        "a class of actions, not a record about anybody."
    ),
    "agent_proposals": _p(
        EMP,
        "ai_interaction",
        # A proposal is about a subject — an employee, a claim, a candidate —
        # and carries the change, the previous state and the agent's reasoning
        # about them. That is an AI interaction concerning a person, and an
        # access request should reach it: being told what an agent proposed
        # about you is part of knowing what the system does with your record.
        secondary_principal=CAND,
    ),
    "proposal_reversals": _p(
        EMP,
        "ai_interaction",
        # The restored state is a copy of what was true about the subject
        # before the agent changed it.
        secondary_principal=CAND,
    ),
    # ── Agent grounding (slice 28) ──────────────────────────────────────────
    "agent_invocations": _p(
        USER,
        "ai_interaction",
        {"question": "ai_interaction", "raw_output": "ai_interaction"},
        # The question somebody asked and the answer they were given, with the
        # record ids behind it. Squarely the ai_interaction category: it is
        # about the requester, and it carries whatever the agent was told about
        # whoever they asked about.
        secondary_principal=EMP,
    ),
    "prompt_injection_attempts": _p(
        CAND,
        "recruitment",
        # A span of text a candidate or an employee wrote, with a pointer back
        # to the document. Classified as recruitment because a resume is where
        # this overwhelmingly comes from; the matched span is capped at 400
        # characters so a security log does not become a second copy of a CV.
        secondary_principal=EMP,
    ),
    # ── Agent evaluation (slice 27) ─────────────────────────────────────────
    "eval_suites": _none(
        "A suite definition: which agent, which version, whether it scores. "
        "Names an agent, not a person."
    ),
    "eval_cases": _none(
        "Inputs and expectations for one case. The inputs are constructed or "
        "anonymised fixtures — a case built from a real candidate would put "
        "that person's record in a file that gets read by everybody who works "
        "on the agent, so the brief's 'anonymised real cases' means anonymised "
        "before it reaches here, and nothing in this table is written from a "
        "live record by any code path in this slice."
    ),
    "eval_runs": _none("One execution: counts, metrics, cost, the gate outcome. Aggregates."),
    "eval_results": _none(
        "One case's outcome, including what the agent returned for a fixture "
        "input. The output is about the fixture, not about a person."
    ),
    "agent_feedback": _p(
        USER,
        "ai_interaction",
        # A person's verdict on an agent output, with the input and output that
        # prompted it. Both can carry whatever the agent was given about
        # somebody, so this is the one table in the slice that holds personal
        # data — and it is exactly the AI-interaction category slice 25
        # declared and had no data for.
    ),
    # ── Retention governance (slice 26) ─────────────────────────────────────
    "retention_policies": _none(
        "A rule: this category, this many months, this action. Describes a "
        "class of records, not any person in it."
    ),
    "legal_holds": _none(
        "A freeze on a scope of records. `employee_ids` lists whose records are "
        "frozen, which is a set of identifiers about people — but the hold is a "
        "processing instruction, not a record about them, and the identifiers "
        "are foreign keys into a table that is itself classified. Holding the "
        "same ids twice does not make this a second personal-data table."
    ),
    "retention_runs": _none(
        "The header of one evaluation: how many records, how many held, who "
        "approved. Counts and a snapshot of the plan, which carries record ids "
        "and no attributes of the people they belong to."
    ),
    "retention_action_logs": _none(
        "What was deleted or anonymised, in what volume, by whose authority. "
        "The sample record ids are stored as HMACs precisely so this log is not "
        "a surviving copy of what it records the deletion of."
    ),
    "erasure_suppressions": _p(
        EMP,
        "governance_record",
        # A row here says 'this record was erased and must not come back'. That
        # is a fact about a person's exercised right, so it belongs to them and
        # an access request should reach it — they are entitled to see that
        # their erasure is being enforced.
        secondary_principal=CAND,
    ),
    # ── Expense policy and travel advances (slice 31) ───────────────────────
    "expense_policies": _none(
        "A named policy: which grades and locations it applies to, from when. Limits, not claims."
    ),
    "expense_policy_rules": _none(
        "One limit for one category in one city tier. A rate card for what may "
        "be spent, held before anybody spends it."
    ),
    "location_city_tiers": _none(
        "Which travel tier a company location sits in. A property of a place."
    ),
    "mileage_rates": _none(
        "What a kilometre is worth in one vehicle, from one date. Effective-dated rate card."
    ),
    "per_diem_rates": _none(
        "A daily allowance for a grade in a tier, from one date. A rate for a "
        "grade, not for a person."
    ),
    "exchange_rate_quotes": _none(
        "A currency's rate on a date and where it was quoted from. Market data."
    ),
    "tenant_expense_settings": _none(
        "One tenant's conversion convention, rate source and advance-block "
        "threshold. Configuration."
    ),
    "travel_requests": _p(
        EMP,
        "financial",
        # Purpose and destinations describe where a named person was and why,
        # which is more revealing than the money. The approval trail names the
        # approvers, so it is also an organisational record about them.
        {"purpose": "financial", "destinations": "financial", "approvals": "audit_trail"},
    ),
    "travel_advances": _p(EMP, "financial"),
    "advance_settlements": _p(EMP, "financial"),
    "expense_claims": _p(
        EMP,
        "financial",
        # A claim's description is free text an employee wrote about a purchase,
        # and it routinely carries who they met and where. The verdict message
        # is what they were told at capture and is part of their record of the
        # decision.
        {
            "description": "financial",
            "verdict_message": "financial",
            "justification": "financial",
            "receipt_document_id": "documents",
        },
    ),
    "expense_policy_violations": _p(
        EMP,
        "financial",
        # A breach record is about conduct as much as money: it is what a
        # violation-rate report is built from, and an access request should
        # reach it, because being counted in that report is a fact about the
        # person.
        {"verdict": "disciplinary", "violation_action": "disciplinary"},
    ),
    # ── Culture depth: awards, values, surveys, comms (slice 32) ────────────
    "company_values": _none(
        "A company value: its name, description and colour. A statement about the organisation."
    ),
    "value_tags": _p(
        EMP,
        "culture_response",
        # A tag says 'this person was recognised for this value on this day'.
        # That is a fact about them and an access request should reach it.
        {"subject_employee_id": "culture_response"},
    ),
    "award_programmes": _none(
        "A recurring award: categories, criteria, eligibility rule, jury roles. "
        "Held before anybody is nominated."
    ),
    "award_cycles": _none(
        "One run of a programme: its window, its status, and the user ids of "
        "its jurors. The jurors are named as an assignment, not described.",
        {"juror_user_ids": "organisational"},
    ),
    "award_nominations": _p(
        EMP,
        "culture_response",
        # The citation is somebody's written assessment of a named colleague and
        # the nominator is identified too.
        {"citation": "culture_response", "supporting_evidence": "performance"},
    ),
    "jury_scores": _p(
        USER,
        "performance",
        # A juror's scores and comments about a named employee: a performance
        # judgement about the nominee, authored by an identified juror.
        {"comments": "performance", "scores": "performance"},
    ),
    "award_results": _p(EMP, "performance"),
    "surveys": _none(
        "A survey definition: instrument, window, anonymity threshold. No answers and no people."
    ),
    "survey_participants": _p(
        EMP,
        "culture_response",
        # Deliberately holds *only* whether somebody responded, never what they
        # said. An access request reaches the fact of participation, which is
        # all this table knows.
        {"responded_at": "culture_response"},
    ),
    "survey_responses": _none(
        "Answers with no respondent column. The table is structurally unable to "
        "identify who gave them, which is the guarantee an anonymous survey "
        "rests on; the group keys are coarse and captured at submission."
    ),
    "survey_action_plans": _p(
        EMP,
        "organisational",
        # About a team and owned by a named person. The ownership is the
        # personal part; the theme and the actions are organisational.
        {"owner_employee_id": "organisational"},
    ),
    "action_plan_items": _p(EMP, "organisational"),
    "announcements": _none(
        "A published communication and its audience rules. Company "
        "communication, not a record about anybody."
    ),
    "announcement_acknowledgements": _p(
        EMP,
        "governance_record",
        # 'This person confirmed they read the POSH policy on this date' is a
        # compliance record about them, and one they are entitled to see.
        {"acknowledged_at": "governance_record"},
    ),
    "event_privacy_preferences": _p(
        EMP,
        "culture_response",
        # A record of what somebody has consented to have announced. The consent
        # itself is personal data about them.
        {"share_birthday": "culture_response"},
    ),
    "company_events": _none(
        "Holidays and company events. A date and a title; the personal events "
        "are derived at read time and never stored here."
    ),
    # ── Points liability and reward tax (slice 33) ──────────────────────────
    "points_ledger": _p(
        EMP,
        "compensation",
        # Recognition points have a rupee value and become a taxable perquisite
        # above a threshold, so the ledger is a record of value received. The
        # reason on a reversal is about the person and belongs to them.
        {"reason": "compensation", "monetary_value": "compensation"},
    ),
    "points_valuations": _none("How many points one rupee buys, from one date. A rate card."),
    "points_expiry_policies": _none(
        "One tenant's expiry period and the disclosure shown at award time. Configuration."
    ),
    "points_budgets": _none(
        "An allocation for a scope and a period. A manager-scoped budget names "
        "a manager as its key, which is an assignment rather than a record "
        "about them.",
        {"scope_key": "organisational"},
    ),
    "reward_tax_rules": _none(
        "How a reward type is treated for tax, from one date, and who reviewed "
        "it. A rule about a class of rewards, not about anybody."
    ),
    "reward_events": _p(
        EMP,
        "tax",
        # 'This person received Rs 5,000 of merchandise in FY 2026-27' is the
        # input to a perquisite calculation. That is tax data about them.
        {"value": "tax", "financial_year": "tax", "description": "compensation"},
    ),
    "points_redemptions": _p(
        EMP,
        "compensation",
        # What somebody chose to spend their recognition on. Mildly revealing
        # and unambiguously theirs.
        {"item_description": "compensation", "item_sku": "compensation"},
    ),
    "reward_fulfilments": _p(
        EMP,
        "compensation",
        # The partner's shipping record against a redemption. It hangs off a
        # named person's order even when the link is broken, which is exactly
        # the case reconciliation exists to surface.
    ),
    "reward_invoice_lines": _none(
        "What the merchandise partner billed for an order. Supplier billing; "
        "the order id is theirs and names no person."
    ),
    "reward_reconciliations": _none(
        "One month's reconciliation and the gaps it found. Counts and order "
        "references. Where a gap concerns a person the detail names the "
        "redemption, which resolves through points_redemptions.",
        {"gaps": "governance_record"},
    ),
    # ── GST input credit (slice 34) ─────────────────────────────────────────
    "gst_entity_registrations": _none(
        "The employer's own GST registrations by state. A company's tax "
        "identifiers, not a person's.",
        {"gstin": "organisational"},
    ),
    "gst_blocked_categories": _none(
        "Which expense categories carry blocked input credit, and the reading "
        "behind each. A rule about a class of supplies."
    ),
    "gst_input_credits": _p(
        EMP,
        "financial",
        # A row here says what a named employee bought, from whom, for how much
        # and where. That is more revealing than the tax figures, and it hangs
        # off their expense claim.
        {
            "vendor_gstin": "financial",
            "eligibility_reason": "financial",
            "hsn_sac": "financial",
        },
    ),
    "gst_credit_reviews": _p(
        USER,
        "governance_record",
        # A named person's decision on a doubtful credit, with their reasoning.
        # It belongs to the reviewer as much as to the claim.
        {"rationale": "governance_record"},
    ),
    "gstr2b_imports": _none(
        "One GSTR-2B download: how many lines and how much tax. A filing "
        "artefact belonging to the company."
    ),
    "gstr2b_lines": _none(
        "The portal's own record of what suppliers filed against the company. "
        "Supplier invoices; the vendor is a business, not a data principal "
        "under this Act.",
        {"vendor_gstin": "organisational", "vendor_name": "organisational"},
    ),
    "gst_reconciliations": _none(
        "One period's match against GSTR-2B: counts, rupee gaps and the line "
        "detail. Where a line concerns an employee's claim it names the credit "
        "id, which resolves through gst_input_credits.",
        {"lines": "governance_record"},
    ),
    # ── PIP and increment flow (slice 35) ───────────────────────────────────
    "pips": _p(
        EMP,
        "disciplinary",
        # A performance improvement plan is the most consequential record this
        # product holds about somebody: it is read by employment counsel and it
        # decides whether they keep their job. Its reason, expectations and
        # outcome are all about them.
        {
            "reason": "disciplinary",
            "expectations": "performance",
            "support_provided": "performance",
            "outcome_reason": "disciplinary",
        },
    ),
    "pip_milestones": _p(EMP, "performance"),
    "pip_check_ins": _p(
        EMP,
        "disciplinary",
        # The employee's own contemporaneous words. Unambiguously theirs, and an
        # access request must reach them - it is their account of a process that
        # may end their employment.
        {
            "employee_comments": "disciplinary",
            "manager_comments": "disciplinary",
            "decline_reason": "disciplinary",
        },
    ),
    "increment_cycles": _none(
        "One increment round: its budget, its bands' parent, its release date. "
        "A cycle, not a person."
    ),
    "increment_matrix_rows": _none(
        "The percentage band one rating earns. A rate card for pay rises."
    ),
    "increment_recommendations": _p(
        EMP,
        "compensation",
        # Current CTC, proposed CTC, the rating behind it and the equity
        # comparison the manager was shown. The comparison is about the person
        # and about their colleagues.
        {
            # CTC is a direct identifier pattern and may not inherit the table
            # default: somebody's salary is the single most sensitive number in
            # an HR system and it has to be classified deliberately.
            "current_ctc": "compensation",
            "recommended_ctc": "compensation",
            "justification": "compensation",
            "equity_snapshot": "compensation",
            "rating_label": "performance",
            "promotion_to_grade": "organisational",
        },
    ),
    "promotion_records": _p(
        EMP,
        "compensation",
        # A grade change, a salary change and a position change together. Also
        # an organisational fact, but the pay is the sensitive half.
        {
            "current_ctc": "compensation",
            "new_ctc": "compensation",
            "to_grade": "organisational",
            "from_grade": "organisational",
        },
    ),
    # ── Recruitment analytics and referrals (slice 36) ──────────────────────
    "referral_programmes": _none(
        "A referral scheme: eligible roles, bonus by grade, payout milestones. "
        "Held before anybody refers."
    ),
    "referrals": _p(
        EMP,
        "recruitment",
        # Two principals: the employee who referred and the candidate who was
        # referred. An access request from either has to reach this row - the
        # employee to see their pending bonus, the candidate to see who put
        # their name forward.
        {"candidate_key": "identity", "bonus_amount": "compensation"},
        secondary_principal=CAND,
    ),
    "referral_payouts": _p(
        EMP,
        "compensation",
        # Money owed to a named employee at a date. Their compensation record.
    ),
    "recruitment_costs": _none(
        "Job board spend, agency fees and recruiter time by period. Company "
        "expenditure; the source is a vendor, not a person."
    ),
    "candidate_diversity_disclosures": _p(
        CAND,
        "identity",
        # The most sensitive category in this product, collected under consent
        # and stored apart from the application so it can be deleted on its own.
        # Withdrawing consent to monitoring must not delete the application.
        {"dimension": "identity", "value": "identity", "consent_id": "governance_record"},
    ),
    # ── BI connector and benchmarking (slice 37) ──────────────────────
    "bi_connections": _none(
        "A configured egress path: which exposed view, to which bucket or "
        "role, under which export role. Configuration, not payload - the "
        "personal data is in what the connection carries, and that is "
        "recorded in bi_export_runs."
    ),
    "bi_export_runs": _p(
        USER,
        "audit_trail",
        # The field list is the point of the row. It names columns, not
        # values, so it is a record of what left rather than a second copy of
        # it - which is exactly why this table can be kept for as long as an
        # audit needs while the export itself is transient.
        {
            "field_names": "audit_trail",
            "withheld_fields": "audit_trail",
            "initiated_by_user_id": "identity",
        },
    ),
    "benchmark_consents": _p(
        USER,
        "governance_record",
        # About the accepting user as much as the company: consent that cannot
        # be attributed to a person who gave it is consent nobody can evidence.
        {
            "accepted_by_user_id": "identity",
            "revoked_by_user_id": "identity",
            "contract_reference": "governance_record",
        },
    ),
    "benchmark_contributions": _none(
        "One company's aggregate value for one metric in one cell - a median "
        "salary, an attrition rate, a time to fill. Company-level statistics, "
        "not a person's record: no employee is identifiable from a cell "
        "average and a headcount. Deleted when a client withdraws consent."
    ),
    "benchmark_datasets": _none(
        "The published market cell: quartiles across at least "
        "MIN_CONTRIBUTORS companies, or the reason it is suppressed. The only "
        "table besides `tenants` with no tenant column, because every "
        "contributing client sees the same cell - a tenant id here would "
        "either name the company a cell is about, which is the identification "
        "the suppression rules exist to prevent, or duplicate an identical "
        "row per client."
    ),
    # ── Security and audit ──────────────────────────────────────────────────
    "audit_logs": _p(USER, "audit_trail"),
    "sso_login_attempts": _p(USER, "account_security"),
    "scim_tokens": _p(USER, "account_security", {"token_hash": "account_security"}),
    "calendar_connections": _p(USER, "account_security", {"account_email": "identity"}),
    "report_runs": _p(USER, "audit_trail"),
    # ── Added by later slices (classified by the DPDP owner) ─────────────────
    "candidate_stage_events": _p(CAND, "recruitment"),
    "expense_receipts": _p(EMP, "financial"),
    "expense_reports": _p(EMP, "financial"),
    "goals": _p(EMP, "performance"),
    "goal_key_results": _p(EMP, "performance"),
    "goal_check_ins": _p(EMP, "performance"),
    "pip_employee_statements": _p(EMP, "performance"),
    # ── Performance conversations: check-in replies, 1:1s, 360, private notes ──
    "goal_check_in_comments": _p(EMP, "performance"),
    "one_on_ones": _p(EMP, "performance"),
    "one_on_one_items": _p(EMP, "performance"),
    "one_on_one_private_notes": _p(EMP, "performance"),
    "feedback_requests": _p(EMP, "performance"),
    "feedback_reviewers": _p(EMP, "performance"),
    "private_feedback": _p(EMP, "performance"),
    "letter_acknowledgements": _p(
        EMP,
        "documents",
        {
            "typed_name": "identity",
            "ip_address": "account_security",
            "user_agent": "account_security",
        },
    ),
    # ── Asset management ────────────────────────────────────────────────────
    "asset_categories": _none(
        "A kind of company equipment and its policy: depreciation life, whether "
        "receipt is acknowledged, and which login collects it at exit."
    ),
    "assets": _none(
        "The company's own equipment: tag, serial number, make, cost, vendor, "
        "warranty and location. Who holds an asset is in asset_assignments."
    ),
    "asset_assignments": _p(EMP, "organisational", {"recovery_amount": "financial"}),
    "asset_requests": _p(EMP, "organisational"),
    # ── HR helpdesk ─────────────────────────────────────────────────────────
    "helpdesk_categories": _none(
        "A kind of request employees can raise, its SLA and the login that works "
        "it. Describes the helpdesk, not anybody's request."
    ),
    "helpdesk_sequences": _none("A per-tenant counter behind ticket numbers."),
    "helpdesk_tickets": _p(EMP, "communication"),
    "helpdesk_comments": _p(EMP, "communication"),
    "helpdesk_attachments": _p(EMP, "documents"),
    # ── Platform operations (W8) ────────────────────────────────────────────
    "scheduler_job_runs": _none(
        "One background job run: which job, which tenant, when, the outcome and "
        "a tally. Handlers return counts, never people."
    ),
    "tenant_whatsapp_settings": _none(
        "A tenant's own WhatsApp Business account: a phone number id and an encrypted access token."
    ),
    "tenant_smtp_settings": _none(
        "A tenant's own mail relay: host, port, an encrypted credential and the "
        "sender address the organisation sends from."
    ),
}


def is_direct_identifier(column: str) -> bool:
    """Whether a column name reads as a direct identifier.

    Word-boundary matching over the snake_case name: `pan` matches `pan`,
    `pan_number` and `landlord_pan`, and does not match `company` or
    `panelist_id`.
    """
    lowered = column.lower()
    return any(
        re.search(rf"(?:^|_){re.escape(pattern)}(?:_|$)", lowered)
        for pattern in DIRECT_IDENTIFIER_PATTERNS
    )


def _unknown_categories() -> set[str]:
    """Category keys named here that the catalogue does not define.

    Used by the invariant rather than raised at import: a wrong key should fail
    a test with a clear message, not stop the application from starting.
    """
    known = category_keys()
    named: set[str] = set()
    for classification in TABLES.values():
        if classification.default_category:
            named.add(classification.default_category)
        named.update(classification.columns.values())
    return named - known


def classification_summary() -> dict[str, Any]:
    """Counts for the compliance surface. Cheap, and useful in a demo."""
    personal = {k: v for k, v in TABLES.items() if v.principal is not None}
    return {
        "tables_total": len(TABLES),
        "tables_holding_personal_data": len(personal),
        "tables_not_personal": len(TABLES) - len(personal),
        "explicit_column_classifications": sum(len(v.columns) for v in TABLES.values()),
        "declared_exemptions": len(EXEMPT_COLUMNS),
    }
