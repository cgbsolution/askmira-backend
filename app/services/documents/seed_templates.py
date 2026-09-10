"""Seed letter templates in standard Indian formats.

═══════════════════════════════════════════════════════════════════════════════
THESE ARE STARTING POINTS, NOT LEGAL ADVICE
═══════════════════════════════════════════════════════════════════════════════

Every template here is marked `is_seed = True`, which drives a visible banner in
the UI: the client's own HR and legal teams must review and adapt these before
issuing anything to a real employee. Employment letters are enforceable
documents and their wording carries obligations that vary by state, by industry
and by the client's own policies.

The formats follow common Indian practice. They are not a substitute for a
lawyer, and this file should not be read as one.
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

from typing import Any

from app.models.documents import FieldSource, LetterCategory

REVIEW_BANNER = (
    "Seed template. Review and adapt with your HR and legal teams before issuing."
)


def _field(token: str, label: str, source: FieldSource, required: bool = True) -> dict[str, Any]:
    return {"token": token, "label": label, "source": source, "required": required}


COMMON_FIELDS = [
    _field("employee.first_name", "First name", FieldSource.EMPLOYEE),
    _field("computed.full_name", "Full name", FieldSource.COMPUTED),
    _field("employee.employee_code", "Employee code", FieldSource.EMPLOYEE),
    _field("employee.designation", "Designation", FieldSource.EMPLOYEE),
    _field("tenant.name", "Company name", FieldSource.TENANT),
    _field("computed.letter_date", "Letter date", FieldSource.COMPUTED),
]


SEED_TEMPLATES: list[dict[str, Any]] = [
    {
        "code": "OFFER_STD",
        "name": "Offer letter (standard)",
        "category": LetterCategory.OFFER,
        "requires_approval": True,
        "approver_roles": ["hr_admin", "tenant_admin"],
        "requires_signature": True,
        "signatory_role": "hr_admin",
        "body_html": """
<p style="text-align:right">{{computed.letter_date}}</p>
<p>Dear {{employee.first_name}},</p>
<h2>Offer of Employment</h2>
<p>We are pleased to offer you the position of <strong>{{employee.designation}}</strong>
with {{tenant.name}}. This offer is made on the terms set out below.</p>

<p><strong>Date of joining:</strong> {{manual.joining_date}}<br/>
<strong>Location:</strong> {{manual.work_location}}</p>

<h3>Compensation</h3>
<p>Your total cost to company will be
<strong>INR {{salary.ctc_annual}}</strong> per annum
({{computed.ctc_in_words}}), structured as follows:</p>

{{computed.ctc_table}}

<p>This offer is contingent on satisfactory background verification and on your
submission of the documents listed separately. It supersedes any prior
discussion, written or verbal.</p>

<p>Please confirm your acceptance by signing below on or before
{{manual.acceptance_deadline}}.</p>

<p>We look forward to welcoming you.</p>
<p>Yours sincerely,<br/>
For {{tenant.name}}</p>
""",
        "fields": [
            *COMMON_FIELDS,
            _field("salary.ctc_annual", "Annual CTC", FieldSource.SALARY),
            _field("computed.ctc_table", "CTC breakdown", FieldSource.COMPUTED),
            _field("computed.ctc_in_words", "CTC in words", FieldSource.COMPUTED),
            _field("manual.joining_date", "Date of joining", FieldSource.MANUAL),
            _field("manual.work_location", "Work location", FieldSource.MANUAL),
            _field("manual.acceptance_deadline", "Acceptance deadline", FieldSource.MANUAL),
        ],
    },
    {
        "code": "APPOINTMENT_STD",
        "name": "Appointment letter (standard)",
        "category": LetterCategory.APPOINTMENT,
        "requires_approval": True,
        "approver_roles": ["hr_admin"],
        "requires_signature": True,
        "signatory_role": "hr_admin",
        "body_html": """
<p style="text-align:right">{{computed.letter_date}}</p>
<p>Dear {{computed.full_name}},</p>
<h2>Letter of Appointment</h2>
<p>Further to your acceptance of our offer, we confirm your appointment as
<strong>{{employee.designation}}</strong> with {{tenant.name}}, effective
{{employee.date_of_joining}}. Your employee code is
<strong>{{employee.employee_code}}</strong>.</p>

<h3>Compensation</h3>
{{computed.ctc_table}}

<h3>Probation</h3>
<p>You will be on probation until {{manual.probation_end_date}}. Your
appointment will be confirmed in writing following a satisfactory review.</p>

<h3>Notice period</h3>
<p>Either party may terminate this employment by giving
{{manual.notice_period}} written notice, or salary in lieu thereof.</p>

<p>Your employment is governed by the company's policies as amended from time
to time.</p>

<p>Yours sincerely,<br/>For {{tenant.name}}</p>
""",
        "fields": [
            *COMMON_FIELDS,
            _field("employee.date_of_joining", "Date of joining", FieldSource.EMPLOYEE),
            _field("computed.ctc_table", "CTC breakdown", FieldSource.COMPUTED),
            _field("manual.probation_end_date", "Probation end date", FieldSource.MANUAL),
            _field("manual.notice_period", "Notice period", FieldSource.MANUAL),
        ],
    },
    {
        "code": "CONFIRMATION_STD",
        "name": "Confirmation letter (standard)",
        "category": LetterCategory.CONFIRMATION,
        "requires_approval": True,
        "approver_roles": ["hr_admin"],
        "body_html": """
<p style="text-align:right">{{computed.letter_date}}</p>
<p>Dear {{employee.first_name}},</p>
<h2>Confirmation of Employment</h2>
<p>We are pleased to confirm your services as
<strong>{{employee.designation}}</strong> with {{tenant.name}} with effect from
{{manual.confirmation_date}}, following the satisfactory completion of your
probation.</p>

<p>All other terms and conditions of your appointment remain unchanged.</p>

<p>We take this opportunity to thank you for your contribution and look forward
to your continued association.</p>

<p>Yours sincerely,<br/>For {{tenant.name}}</p>
""",
        "fields": [
            *COMMON_FIELDS,
            _field("manual.confirmation_date", "Confirmation effective date", FieldSource.MANUAL),
        ],
    },
    {
        "code": "RELIEVING_STD",
        "name": "Relieving letter (standard)",
        "category": LetterCategory.RELIEVING,
        "requires_approval": True,
        "approver_roles": ["hr_admin"],
        "body_html": """
<p style="text-align:right">{{computed.letter_date}}</p>
<p>Dear {{computed.full_name}},</p>
<h2>Relieving Letter</h2>
<p>This is to confirm that you were employed with {{tenant.name}} as
<strong>{{employee.designation}}</strong> from {{employee.date_of_joining}} to
{{employee.date_of_exit}}, a period of {{computed.tenure}}.</p>

<p>Your resignation has been accepted and you stand relieved of your duties with
effect from the close of business on {{employee.date_of_exit}}. We confirm that
you have been cleared of all dues and obligations to the company.</p>

<p>We wish you the very best in your future endeavours.</p>

<p>Yours sincerely,<br/>For {{tenant.name}}</p>
""",
        "fields": [
            *COMMON_FIELDS,
            _field("employee.date_of_joining", "Date of joining", FieldSource.EMPLOYEE),
            _field("employee.date_of_exit", "Date of exit", FieldSource.EMPLOYEE),
            _field("computed.tenure", "Tenure", FieldSource.COMPUTED),
        ],
    },
    {
        "code": "EXPERIENCE_STD",
        "name": "Experience certificate (standard)",
        "category": LetterCategory.EXPERIENCE,
        "requires_approval": False,
        "body_html": """
<p style="text-align:right">{{computed.letter_date}}</p>
<h2 style="text-align:center">To Whom It May Concern</h2>
<p>This is to certify that {{computed.full_name}} (Employee Code
{{employee.employee_code}}) was employed with {{tenant.name}} from
{{employee.date_of_joining}} to {{employee.date_of_exit}}, a period of
{{computed.tenure}}.</p>

<p>At the time of leaving, {{employee.first_name}} held the position of
<strong>{{employee.designation}}</strong>.</p>

<p>We found {{employee.first_name}} to be sincere and diligent, and we wish
{{employee.first_name}} success in future endeavours.</p>

<p>Yours sincerely,<br/>For {{tenant.name}}</p>
""",
        "fields": [
            *COMMON_FIELDS,
            _field("employee.date_of_joining", "Date of joining", FieldSource.EMPLOYEE),
            _field("employee.date_of_exit", "Date of exit", FieldSource.EMPLOYEE),
            _field("computed.tenure", "Tenure", FieldSource.COMPUTED),
        ],
    },
    {
        "code": "SALARY_CERT_STD",
        "name": "Salary certificate (standard)",
        "category": LetterCategory.SALARY_CERTIFICATE,
        "requires_approval": False,
        "body_html": """
<p style="text-align:right">{{computed.letter_date}}</p>
<h2 style="text-align:center">Salary Certificate</h2>
<p>This is to certify that {{computed.full_name}} (Employee Code
{{employee.employee_code}}) is employed with {{tenant.name}} as
<strong>{{employee.designation}}</strong> since
{{employee.date_of_joining}}.</p>

<p>The current annual cost to company is <strong>INR
{{salary.ctc_annual}}</strong> ({{computed.ctc_in_words}}), broken down as
follows:</p>

{{computed.ctc_table}}

<p>This certificate is issued at the request of the employee for
{{manual.purpose}}.</p>

<p>Yours sincerely,<br/>For {{tenant.name}}</p>
""",
        "fields": [
            *COMMON_FIELDS,
            _field("employee.date_of_joining", "Date of joining", FieldSource.EMPLOYEE),
            _field("salary.ctc_annual", "Annual CTC", FieldSource.SALARY),
            _field("computed.ctc_table", "CTC breakdown", FieldSource.COMPUTED),
            _field("computed.ctc_in_words", "CTC in words", FieldSource.COMPUTED),
            _field("manual.purpose", "Purpose of the certificate", FieldSource.MANUAL),
        ],
    },
]
