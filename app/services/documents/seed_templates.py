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

STATUTORY_FIELDS = [
    _field("statutory.date_of_birth", "Date of birth", FieldSource.EMPLOYEE),
    _field("statutory.gender", "Gender", FieldSource.EMPLOYEE),
    _field("statutory.father_or_spouse_name", "Father's or spouse's name", FieldSource.EMPLOYEE),
]


SEED_TEMPLATES: list[dict[str, Any]] = [
    {
        "code": "OFFER_STD",
        "name": "Offer letter (standard)",
        "category": LetterCategory.OFFER,
        "requires_approval": True,
        "approver_roles": ["admin"],
        "requires_signature": True,
        "signatory_role": "admin",
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
        "approver_roles": ["admin"],
        "requires_signature": True,
        "signatory_role": "admin",
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
        "approver_roles": ["admin"],
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
        "approver_roles": ["admin"],
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
    {
        "code": "INCREMENT_STD",
        "name": "Increment letter (standard)",
        "category": LetterCategory.PROMOTION,
        # Issued by releasing an increment cycle, whose recommendations were
        # already approved; a second approval of the letter would only delay it.
        "requires_approval": False,
        "body_html": """
<p style="text-align:right">{{computed.letter_date}}</p>
<p>Dear {{employee.first_name}},</p>
<h2>Revision of Compensation</h2>
<p>In recognition of your contribution, we are pleased to revise your annual cost
to company from INR {{manual.previous_ctc}} to <strong>INR
{{salary.ctc_annual}}</strong> ({{computed.ctc_in_words}}), an increase of
{{manual.increase_percentage}}%, with effect from {{salary.effective_from}}.</p>

<p>Your revised compensation is broken down as follows:</p>

{{computed.ctc_table}}

<p>All other terms and conditions of your employment remain unchanged. This
information is confidential to you.</p>

<p>Yours sincerely,<br/>For {{tenant.name}}</p>
""",
        "fields": [
            *COMMON_FIELDS,
            _field("salary.ctc_annual", "Revised annual CTC", FieldSource.SALARY),
            _field("salary.effective_from", "Effective from", FieldSource.SALARY),
            _field("computed.ctc_table", "CTC breakdown", FieldSource.COMPUTED),
            _field("computed.ctc_in_words", "CTC in words", FieldSource.COMPUTED),
            _field("manual.previous_ctc", "Previous annual CTC", FieldSource.MANUAL),
            _field("manual.increase_percentage", "Increase (%)", FieldSource.MANUAL),
        ],
    },
    # -- Statutory declarations a joiner makes, generated from their profile --
    {
        "code": "FORM11_STD",
        "name": "EPF Declaration (Form 11)",
        "category": LetterCategory.CUSTOM,
        "requires_approval": False,
        "body_html": """
<h2 style="text-align:center">Form No. 11 (Declaration Form)</h2>
<p style="text-align:center">(To be retained by the employer for future reference)<br/>
Employees' Provident Fund Organisation - Employees' Provident Funds Scheme, 1952
(Paragraph 34 &amp; 57) and Employees' Pension Scheme, 1995 (Paragraph 24)</p>
<table class="ctc-table" cellspacing="0" cellpadding="6"><tbody>
<tr><td>1. Name of the member</td><td>{{computed.full_name}}</td></tr>
<tr><td>2. Father's / spouse's name</td><td>{{statutory.father_or_spouse_name}}</td></tr>
<tr><td>3. Date of birth</td><td>{{statutory.date_of_birth}}</td></tr>
<tr><td>4. Gender</td><td>{{statutory.gender}}</td></tr>
<tr><td>5. Marital status</td><td>{{statutory.marital_status}}</td></tr>
<tr><td>6. Email</td><td>{{statutory.email}}</td></tr>
<tr><td>7. Mobile</td><td>{{statutory.mobile}}</td></tr>
<tr><td>8. Earlier a member of the EPF Scheme, 1952 / EPS, 1995</td>
<td>{{statutory.previous_pf_member}}</td></tr>
<tr><td>9. Universal Account Number (if earlier a member)</td><td>{{statutory.uan}}</td></tr>
<tr><td>10. Date of joining this establishment</td><td>{{employee.date_of_joining}}</td></tr>
<tr><td>11. Employer</td><td>{{tenant.name}}</td></tr>
</tbody></table>
<h3>Undertaking</h3>
<p>I certify that all the information given above is true to the best of my knowledge
and belief. I undertake to inform the employer of any change in it. Where I was a
member earlier, I authorise the transfer of my accumulations to my account under this
establishment.</p>
<p>Date: {{computed.letter_date}}<br/>Signature of the member: acknowledged
electronically by {{computed.full_name}} ({{employee.employee_code}})</p>
""",
        "fields": [
            *COMMON_FIELDS,
            *STATUTORY_FIELDS,
            _field("employee.date_of_joining", "Date of joining", FieldSource.EMPLOYEE),
            _field("statutory.marital_status", "Marital status", FieldSource.EMPLOYEE, False),
            _field("statutory.email", "Email", FieldSource.EMPLOYEE, False),
            _field("statutory.mobile", "Mobile", FieldSource.EMPLOYEE, False),
            _field("statutory.previous_pf_member", "Earlier PF member", FieldSource.EMPLOYEE),
            _field("statutory.uan", "UAN", FieldSource.EMPLOYEE, False),
        ],
    },
    {
        "code": "FORM2_STD",
        "name": "PF and EPS nomination (Form 2)",
        "category": LetterCategory.CUSTOM,
        "requires_approval": False,
        "body_html": """
<h2 style="text-align:center">Form 2 (Revised)</h2>
<p style="text-align:center">Nomination and Declaration Form - Employees' Provident
Funds Scheme, 1952 (Paragraphs 33 &amp; 61(1)) and Employees' Pension Scheme, 1995
(Paragraph 18)</p>
<table class="ctc-table" cellspacing="0" cellpadding="6"><tbody>
<tr><td>Name</td><td>{{computed.full_name}}</td></tr>
<tr><td>Father's / spouse's name</td><td>{{statutory.father_or_spouse_name}}</td></tr>
<tr><td>Date of birth</td><td>{{statutory.date_of_birth}}</td></tr>
<tr><td>Gender</td><td>{{statutory.gender}}</td></tr>
<tr><td>Marital status</td><td>{{statutory.marital_status}}</td></tr>
<tr><td>Account / UAN</td><td>{{statutory.uan}}</td></tr>
<tr><td>Permanent address</td><td>{{statutory.permanent_address}}</td></tr>
</tbody></table>
<h3>Part A (EPF)</h3>
<p>I hereby nominate the person(s) below to receive the amount standing to my credit
in the Employees' Provident Fund in the event of my death, in the shares shown.</p>
{{computed.pf_nominees_table}}
<h3>Part B (EPS) - family members eligible for pension</h3>
{{computed.family_table}}
<p>I undertake to submit a fresh nomination on acquiring a family or on any change in
it.</p>
<p>Date: {{computed.letter_date}}<br/>Signature of the member: acknowledged
electronically by {{computed.full_name}} ({{employee.employee_code}})</p>
<p>Certified by the employer, {{tenant.name}}.</p>
""",
        "fields": [
            *COMMON_FIELDS,
            *STATUTORY_FIELDS,
            _field("statutory.marital_status", "Marital status", FieldSource.EMPLOYEE, False),
            _field("statutory.uan", "UAN", FieldSource.EMPLOYEE, False),
            _field("statutory.permanent_address", "Permanent address", FieldSource.EMPLOYEE),
            _field("computed.pf_nominees_table", "PF nominees", FieldSource.COMPUTED),
            _field("computed.family_table", "Family members", FieldSource.COMPUTED),
        ],
    },
    {
        "code": "FORMF_STD",
        "name": "Gratuity nomination (Form F)",
        "category": LetterCategory.CUSTOM,
        "requires_approval": False,
        "body_html": """
<h2 style="text-align:center">Form 'F'</h2>
<p style="text-align:center">[See sub-rule (1) of Rule 6] - Nomination under the
Payment of Gratuity Act, 1972</p>
<p>To {{tenant.name}},</p>
<p>I, {{computed.full_name}}, whose particulars are given below, hereby nominate the
person(s) mentioned below to receive the gratuity payable after my death, as also the
gratuity standing to my credit in the event of my death before it becomes payable, and
direct that it be distributed among them in the proportion indicated against their
names.</p>
{{computed.gratuity_nominees_table}}
<p>I certify that the person(s) mentioned are member(s) of my family within the meaning
of clause (h) of Section 2 of the Act, and undertake to submit a fresh nomination in
case I acquire a family, or a member of my family dies.</p>
<table class="ctc-table" cellspacing="0" cellpadding="6"><tbody>
<tr><td>Name of the employee</td><td>{{computed.full_name}}</td></tr>
<tr><td>Gender</td><td>{{statutory.gender}}</td></tr>
<tr><td>Date of birth</td><td>{{statutory.date_of_birth}}</td></tr>
<tr><td>Date of appointment</td><td>{{employee.date_of_joining}}</td></tr>
<tr><td>Designation</td><td>{{employee.designation}}</td></tr>
<tr><td>Permanent address</td><td>{{statutory.permanent_address}}</td></tr>
</tbody></table>
<p>Date: {{computed.letter_date}}<br/>Signature of the employee: acknowledged
electronically by {{computed.full_name}} ({{employee.employee_code}})</p>
""",
        "fields": [
            *COMMON_FIELDS,
            *STATUTORY_FIELDS,
            _field("employee.date_of_joining", "Date of appointment", FieldSource.EMPLOYEE),
            _field("statutory.permanent_address", "Permanent address", FieldSource.EMPLOYEE),
            _field("computed.gratuity_nominees_table", "Gratuity nominees", FieldSource.COMPUTED),
        ],
    },
    {
        "code": "ESI_DECL_STD",
        "name": "ESI declaration (Form 1)",
        "category": LetterCategory.CUSTOM,
        "requires_approval": False,
        "body_html": """
<h2 style="text-align:center">Employees' State Insurance Corporation</h2>
<h3 style="text-align:center">Declaration Form (Form 1)</h3>
<table class="ctc-table" cellspacing="0" cellpadding="6"><tbody>
<tr><td>Insurance number</td><td>{{statutory.esi_number}}</td></tr>
<tr><td>Name</td><td>{{computed.full_name}}</td></tr>
<tr><td>Father's / husband's name</td><td>{{statutory.father_or_spouse_name}}</td></tr>
<tr><td>Date of birth</td><td>{{statutory.date_of_birth}}</td></tr>
<tr><td>Gender</td><td>{{statutory.gender}}</td></tr>
<tr><td>Marital status</td><td>{{statutory.marital_status}}</td></tr>
<tr><td>Present address</td><td>{{statutory.current_address}}</td></tr>
<tr><td>Permanent address</td><td>{{statutory.permanent_address}}</td></tr>
<tr><td>Employer</td><td>{{tenant.name}}</td></tr>
<tr><td>Date of appointment</td><td>{{employee.date_of_joining}}</td></tr>
</tbody></table>
<h3>Particulars of family members</h3>
{{computed.family_table}}
<p>I hereby declare that the particulars given above are correct to the best of my
knowledge and belief, and undertake to inform the Corporation of any change within
fifteen days.</p>
<p>Date: {{computed.letter_date}}<br/>Signature of the insured person: acknowledged
electronically by {{computed.full_name}} ({{employee.employee_code}})</p>
""",
        "fields": [
            *COMMON_FIELDS,
            *STATUTORY_FIELDS,
            _field("employee.date_of_joining", "Date of appointment", FieldSource.EMPLOYEE),
            _field("statutory.esi_number", "ESI insurance number", FieldSource.EMPLOYEE, False),
            _field("statutory.marital_status", "Marital status", FieldSource.EMPLOYEE, False),
            _field("statutory.current_address", "Present address", FieldSource.EMPLOYEE),
            _field("statutory.permanent_address", "Permanent address", FieldSource.EMPLOYEE),
            _field("computed.family_table", "Family members", FieldSource.COMPUTED),
        ],
    },
]
