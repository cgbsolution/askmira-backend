"""The letters every workspace starts with.

Offer letter (for client deployments and for internal hires), experience
letter, promotion letter with its salary annexure, and the employee joining
form: the wording CGBiz Solutions' HR team uses, with every name, date, figure
and company detail replaced by a field that is filled in when a letter is
issued. The company's own name, address and signatory come from the letterhead
(Documents -> Letterhead), so the same templates serve any workspace.

They are ordinary templates once installed: HR edits the wording (each save is
a new version), switches one off, or adds more beside them. They are installed
once per workspace (`tenant.settings["letter_defaults"]` records the version),
so a template HR deleted or renamed does not come back.

Fields marked `default` are typed by HR at issuance but start filled in from
the record — "Notice period" starts as the employee's notice period in words.
"""

from __future__ import annotations

from typing import Any

from app.models.documents import FieldSource, LetterCategory

#: Bump when a new default letter is added; only the missing ones are installed.
DEFAULTS_VERSION = 1

E, S, T, M, C = (
    FieldSource.EMPLOYEE,
    FieldSource.SALARY,
    FieldSource.TENANT,
    FieldSource.MANUAL,
    FieldSource.COMPUTED,
)


def _f(
    token: str,
    label: str,
    source: FieldSource,
    *,
    required: bool = True,
    default: str | None = None,
) -> dict[str, Any]:
    return {
        "token": token,
        "label": label,
        "source": source,
        "required": required,
        "source_path": default,
    }


_SIGN_OFF = """
<table class="keep" cellspacing="0" cellpadding="0"><tr><td>
<p><strong>Sincerely,</strong></p>
{{computed.signature}}
<p><strong>{{company.signatory_title}},<br/>{{company.name}}</strong></p>
</td></tr></table>
"""

_ACKNOWLEDGEMENT = """
<p>&nbsp;</p>
<table class="keep" cellspacing="0" cellpadding="0"><tr><td>
<p><strong>Employee Acknowledgement</strong></p>
<p>I hereby acknowledge receipt of this <strong>{what}</strong> and confirm my acceptance of the
terms and conditions mentioned herein.</p>
<p><strong>Employee Signature: __________________<br/>Date: __________________</strong></p>
</td></tr></table>
"""

_TERMS = """
<table cellspacing="0" cellpadding="3" style="width:75%; margin:4pt 0 8pt 0">
<tr><td width="30%"><strong>Designation</strong></td><td width="4%"><strong>:</strong></td>
<td>{{employee.designation}}</td></tr>
<tr><td><strong>Cost to Company</strong></td><td><strong>:</strong></td>
<td>Rs. {{salary.ctc_annual}} per annum</td></tr>
<tr><td><strong>Probation Period</strong></td><td><strong>:</strong></td>
<td>{{manual.probation_period}}</td></tr>
<tr><td><strong>Expected DOJ</strong></td><td><strong>:</strong></td>
<td>{{employee.date_of_joining}}</td></tr>
<tr><td><strong>Location</strong></td><td><strong>:</strong></td>
<td>{{employee.location}}</td></tr>
</table>
"""

_OFFER_FIELDS = [
    _f("computed.letter_date", "Letter date", C),
    _f("employee.salutation", "Mr. / Ms.", E),
    _f("employee.full_name", "Full name", E),
    _f("employee.designation", "Designation", E),
    _f("salary.ctc_annual", "CTC per year", S),
    _f(
        "manual.probation_period",
        "Probation period",
        M,
        default="employee.probation_period",
    ),
    _f("employee.date_of_joining", "Expected date of joining", E),
    _f("employee.location", "Location", E),
    _f("manual.notice_period", "Notice period", M, default="employee.notice_period"),
    _f("company.name", "Company name", T),
    _f("computed.salary_annexure", "Salary annexure", C),
    _f("computed.signature", "Signature", C, required=False),
]

OFFER_CLIENT = (
    """
<p><strong>Date : {{computed.letter_date}}</strong></p>
<p><strong>Dear {{employee.salutation}} {{employee.full_name}},</strong></p>
<p>Thank you for showing interest in {{company.name}}!</p>
<p><strong>We are pleased to offer you employment with {{company.name}} on the following terms
and conditions.</strong></p>
"""
    + _TERMS
    + """
<p><strong>You will be governed by the policies and procedures applicable to {{company.name}} in
accordance with the policies applicable to your assigned work location.</strong></p>

<p><strong><u>Offer Acceptance:</u></strong></p>
<p style="text-align:justify">Please submit the duplicate copy of this offer letter duly signed by
you as a token of acceptance within one week. After submitting your resignation to your current
employer, kindly share a copy of the same and communicate your confirmed date of joining.</p>

<p><strong><u>Leaves:</u></strong></p>
<p style="text-align:justify">After successful completion of the probation period, you will be
entitled to earned leave at the rate of 1.25 days for each completed month of service. Leave may be
accumulated during the calendar year; however, all accumulated leave must be utilized within the
same calendar year, and no carry forward will be permitted.</p>

<p><strong><u>Notice Period:</u></strong></p>
<p style="text-align:justify">The service with the Company may be terminated by the employee giving
{{manual.notice_period}}&rsquo; notice in writing regardless you are being on probation or as a
confirmed employee. However, in cases of performance issues, involving unethical conduct, or
integrity violations, the notice period will be immediate.</p>

<p><strong><u>Exclusive employment:</u></strong></p>
<p style="text-align:justify">You will agree to work exclusively for the company while in its
employment and abide by the company policy on exclusive employment and non-compete.</p>
<p style="text-align:justify">Your individual remuneration is purely a fact between you and the
company and has been arrived at based on your professional merit and the company&rsquo;s
prevailing compensation guidelines. You are required to maintain this information and any changes
hereafter, as strictly personal and confidential.</p>

<p><strong>Kindly take a printout of this letter, sign it, and return the duplicate copy as a token
of your acceptance of the terms and conditions mentioned herein.</strong></p>
"""
    + _SIGN_OFF
    + """
<div class="page-break"></div>
<h2 style="text-align:center">Annexure</h2>
{{computed.salary_annexure}}
"""
    + _ACKNOWLEDGEMENT.format(what="offer letter")
)

OFFER_INTERNAL = (
    """
<h2 style="text-align:center">Offer Letter</h2>
<p><strong>Date : {{computed.letter_date}}</strong></p>
<p><strong>Dear {{employee.salutation}} {{employee.full_name}},</strong></p>
<p>Thank you for showing interest in {{company.name}}!</p>
<p><strong>We are pleased to offer you employment with {{company.name}} on the following terms
and conditions.</strong></p>
"""
    + _TERMS
    + """
<p><strong>You will be governed by the policies and procedures applicable to {{company.name}} in
accordance to the assigned based location.</strong></p>

<p><strong><u>Offer Acceptance:</u></strong></p>
<p style="text-align:justify">Please submit the duplicate copy of this offer letter duly signed by
you as a token of acceptance should reach within a week. After you submit your resignation to the
current employer, please send us a copy of the same and communicate your confirmed Date of
Joining.</p>

<p><strong><u>Leaves:</u></strong></p>
<p style="text-align:justify">After successful completion of the probation period, you will be
entitled to earned leave at the rate of 1.25 days for each completed month of service. Leave may be
accumulated during the calendar year; however, all accumulated leave must be utilized within the
same calendar year, and no carry forward will be permitted.</p>
<p style="text-align:justify">The company&rsquo;s working days are Monday to Friday.
Company-declared holidays and weekly offs may be prefixed or suffixed to approved leave. Weekly offs
(Saturdays &amp; Sundays) and company-paid holidays will not be included in the calculation of
leave.</p>

<p><strong><u>Notice Period:</u></strong></p>
<p style="text-align:justify">The service with the Company may be terminated by the employee giving
{{manual.notice_period}}&rsquo; notice in writing regardless you are being on probation or as a
confirmed employee. However, in cases of performance issues, involving unethical conduct, or
integrity violations, the notice period will be immediate.</p>

<p><strong><u>Exclusive employment:</u></strong></p>
<p style="text-align:justify">You will agree to work exclusively for the company while in its
employment and abide by the company policy on exclusive employment and non-compete.</p>
<p style="text-align:justify">Your individual remuneration is purely a fact between you and the
company and has been arrived at based on your professional merit and the company&rsquo;s
prevailing compensation guidelines. You are required to maintain this information and any changes
hereafter, as strictly personal and confidential.</p>

<p><strong>Kindly take a printout of this letter, sign it, and return the duplicate copy as a token
of your acceptance of the terms and conditions mentioned herein.</strong></p>
"""
    + _SIGN_OFF
    + """
<div class="page-break"></div>
<h2 style="text-align:center">Annexure</h2>
{{computed.salary_annexure}}
"""
    + _ACKNOWLEDGEMENT.format(what="offer letter")
)

EXPERIENCE = """
<p style="text-align:right"><strong>Date : {{computed.letter_date}}</strong></p>
<h2 style="text-align:center">To whom so ever It may concern</h2>
<p>&nbsp;</p>
<p style="text-align:justify">This is to certify that <strong>{{employee.salutation}}
{{employee.full_name}}</strong>, holding Employee ID: <strong>{{employee.employee_code}}</strong>,
has worked with us from <strong>{{employee.date_of_joining}} to {{employee.date_of_exit}}</strong>
and was designated as <strong>&ldquo;{{employee.designation}}&rdquo;</strong> at the time of
leaving the organization and there are no claims / no due with our company.</p>
<p style="text-align:justify">During the tenure we found {{employee.first_name}} to be regular,
honest, and diligent in assigned duties and responsibilities.</p>
<p>&nbsp;</p>
<table class="keep" cellspacing="0" cellpadding="0"><tr><td>
<p><strong>Yours Sincerely,</strong></p>
{{computed.signature}}
<p><strong>{{company.signatory_title}}<br/>{{company.name}}</strong></p>
</td></tr></table>
"""

PROMOTION = """
<h2 style="text-align:center">Promotion Letter</h2>
<p><strong>Date : {{computed.letter_date}}</strong></p>
<p>{{employee.full_name}}<br/>{{employee.location}}</p>
<p><strong>Subject: Promotion to {{manual.designation}}</strong></p>
<p>Dear {{employee.first_name}},</p>
<p style="text-align:justify">We are delighted to formally inform you that, in recognition of your
exceptional performance, dedication, and continuous professional growth, you have been promoted to
the position of <strong>{{manual.designation}}</strong>, effective
<strong>{{salary.effective_from}}</strong>.</p>
<p style="text-align:justify">Since the day you joined, your contributions to {{company.name}} have
been outstanding, and your ability to lead projects and streamline processes has played a vital
role in company success. Your passion for excellence, teamwork, and problem-solving has truly made
a significant impact on the success of the organization.</p>
<p style="text-align:justify">We believe your skills and experience will continue to drive success
in your new position and contribute to the growth of {{company.name}}.</p>
<p style="text-align:justify">With this promotion, your <strong>revised CTC will be Rs.
{{salary.ctc_annual}} per annum</strong>, and your benefits will remain in accordance with company
policies. Additionally, your <strong>notice period has been revised to
{{manual.notice_period}}</strong>.</p>
<p style="text-align:justify">We are confident that you will meet and exceed the expectations of
your new role. We look forward to your continued success as you take on greater responsibilities
and contribute further to the organization&rsquo;s growth.</p>
<p style="text-align:justify">Please take the time to review the attached promotion letter and feel
free to reach out to HR Department, if you have any questions or need further clarification.</p>
<p style="text-align:justify">Once again, congratulations on this well-deserved achievement, and
thank you for your hard work and dedication to <strong>{{company.name}}.</strong></p>
<p>We look forward to seeing your continued growth and success</p>
<table class="keep" cellspacing="0" cellpadding="0"><tr><td>
<p><strong>Sincerely</strong></p>
{{computed.signature}}
<p><strong>{{company.signatory_title}}<br/>{{company.name}}</strong></p>
</td></tr></table>
<div class="page-break"></div>
<h2 style="text-align:center">Annexure</h2>
{{computed.salary_annexure}}
""" + _ACKNOWLEDGEMENT.format(what="promotion letter")


def _line(label: str, value: str, note: str = "", label_width: int = 24) -> str:
    """One ruled line of a fill-in form, pre-filled where the record knows it.

    Every cell has a width and the line always holds a space: a renderer
    collapses an empty cell, which would print a blank as a dash.
    """
    line_width = 72 - label_width if label_width < 60 else 100 - label_width
    note_width = 22 if note else 0
    rest = 100 - label_width - line_width - note_width
    cells = (
        f'<td width="{label_width}%">{label}</td>'
        f'<td class="line" width="{line_width}%">{value}&nbsp;</td>'
        + (f'<td width="{note_width}%">&nbsp;{note}</td>' if note else "")
        + (f'<td width="{rest}%">&nbsp;</td>' if rest > 0 else "")
    )
    return f'<table class="form" cellspacing="0"><tr>{cells}</tr></table>'


def _pair(*items: tuple[str, str, int, int]) -> str:
    """Several labelled lines side by side: (label, value, label width, line width)."""
    cells = "".join(
        f'<td width="{lw}%">{label}</td><td class="line" width="{vw}%">{value}&nbsp;</td>'
        for label, value, lw, vw in items
    )
    rest = 100 - sum(lw + vw for _, _, lw, vw in items)
    if rest > 0:
        cells += f'<td width="{rest}%">&nbsp;</td>'
    return f'<table class="form" cellspacing="0"><tr>{cells}</tr></table>'


JOINING_FORM = (
    """
<p><strong>Date: {{computed.letter_date}}</strong></p>
<h2 style="text-align:center">Employee Joining Form</h2>
"""
    + _line("Full Name:", "{{employee.full_name}}", "<strong>(as per PAN Card)</strong>")
    + _line("Employee ID (To be assigned by HR):", "{{employee.employee_code}}", "", 44)
    + _line("Date of Birth:", "{{statutory.date_of_birth}}")
    + _line("Gender:", "{{statutory.gender}}")
    + _line("Marital Status:", "{{profile.marital_status}}")
    + _line("Husband/Wife Name :", "{{profile.spouse_name}}", "<strong>(as per PAN)</strong>", 28)
    + _line("Father&rsquo;s Name :", "{{profile.father_name}}", "<strong>(as per PAN)</strong>")
    + _line("Mother&rsquo;s Name :", "{{profile.mother_name}}")
    + _line("Blood Group :", "{{profile.blood_group}}")
    + _line("Permanent Address:", "{{profile.permanent_address}}", "", 28)
    + _pair(
        ("City:", "{{profile.permanent_city}}", 8, 24),
        ("State:", "{{profile.permanent_state}}", 9, 17),
        ("ZIP Code:", "{{profile.permanent_pincode}}", 14, 12),
    )
    + _line("Temporary Address:", "{{profile.current_address}}", "", 28)
    + _pair(
        ("City:", "{{profile.current_city}}", 8, 24),
        ("State:", "{{profile.current_state}}", 9, 17),
        ("ZIP Code:", "{{profile.current_pincode}}", 14, 12),
    )
    + _pair(
        ("Phone Number:", "{{statutory.mobile}}", 20, 24),
        ("Email:", "{{profile.personal_email}}", 9, 33),
    )
    + _line("Emergency Contact Name:", "{{profile.emergency_name}}", "", 36)
    + _line("Emergency Contact Relation:", "{{profile.emergency_relation}}", "", 38)
    + _line("Emergency Contact Phone Number:", "{{profile.emergency_phone}}", "", 44)
    + """<p style="margin-top:10pt"><strong>Educational Details:</strong></p>"""
    + _pair(
        ("Highest Education :", "{{profile.highest_education}}", 25, 25),
        ("Specialization :", "{{profile.specialisation}}", 19, 31),
    )
    + """
"""
    + _line("Total Experience till date :", "{{profile.total_experience}}", "", 38)
    + """
<p style="margin-top:10pt"><strong>Work Experience (Previous) :</strong></p>
{{computed.previous_employment_table}}
<table class="grid" cellspacing="0"><thead><tr>
<th>Immediate Reporting Manager</th><th>HR Manager</th><th>Last Drawn Salary</th>
<th>Reason for Leaving</th></tr></thead>
<tbody><tr><td><strong>Name :<br/>Contact No. :<br/>Email ID :</strong></td>
<td><strong>Name :<br/>Contact No. :<br/>Email ID :</strong></td><td>&nbsp;</td><td>&nbsp;</td>
</tr></tbody></table>
<p style="margin-top:10pt"><strong>Bank Details:</strong></p>
"""
    + _line("Bank Name:", "{{profile.bank_name}}")
    + _line("Branch Name:", "")
    + _line("Account Holder Name:", "{{profile.account_holder_name}}", "", 32)
    + _line("Account Number:", "{{profile.account_number}}")
    + _line("IFSC Code:", "{{profile.ifsc}}")
    + _line("Type of Account (Checking/Savings):", "", "", 48)
    + """<p style="margin-top:10pt"><strong>PF &amp; ESI Details:</strong></p>"""
    + _line("UAN Number:", "{{statutory.uan}}")
    + _line("ESI Number:", "{{statutory.esi_number}}")
    + """
<div class="page-break"></div>
<h3>Employment Documents:</h3>
<ol>
<li><strong>Signed copy of your Employment Offer Letter/Contract. (upon receiving the Offer
Letter)</strong></li>
<li><strong>Photocopy of your Proof of Identification (e.g., passport, driver&rsquo;s license,
Voter Card, Birth Certificate).</strong></li>
<li><strong>Aadhar Card / National ID</strong></li>
<li><strong>PAN Card</strong></li>
<li><strong>Completed Tax Forms (form 16) for Payroll Processing.</strong></li>
<li><strong>Last 3 Months Pay slips and Bank Statement</strong></li>
<li><strong>All Educational Documents (Class 10<sup>th</sup>, 12<sup>th</sup>, Graduation and Post
Graduation)</strong></li>
<li><strong>Last Organization/s Relieving Letter (all Organizations)</strong></li>
<li><strong>Last organization&rsquo;s Resignation Acceptance Email</strong></li>
<li><strong>Other relevant documents as mentioned in your offer letter.</strong></li>
<li><strong>Cancelled Cheque.</strong></li>
<li><strong>Passport</strong></li>
<li><strong>Passport Size Photo.</strong></li>
<li><strong>Updated Resume</strong></li>
</ol>
<p><strong>Please provide the following documents to ensure HR compliance:</strong></p>
<ol>
<li><strong>Photocopy of any Work Authorization documents (if applicable).</strong></li>
<li><strong>Proof of Address (e.g., utility bill, rental agreement).</strong></li>
<li><strong>By signing below, you confirm that all the information provided above is accurate, and
you understand the importance of complying with HR requirements and regulations.</strong></li>
</ol>
<p>&nbsp;</p>
<table cellspacing="0" cellpadding="4" style="width:100%">
<tr><td width="65%"><strong>Employee Signature: ________________________</strong></td>
<td><strong>Date: ______________</strong></td></tr>
<tr><td>&nbsp;</td><td></td></tr>
<tr><td><strong>HR Representative Signature: __________________</strong></td>
<td><strong>Date: ______________</strong></td></tr>
</table>
<p>&nbsp;</p>
<p><strong>Note: All information provided will be treated confidentially and in accordance with
applicable privacy laws and regulations.</strong></p>
"""
)


DEFAULT_TEMPLATES: list[dict[str, Any]] = [
    {
        "code": "OFFER_CLIENT",
        "name": "Offer Letter (Clients)",
        "category": LetterCategory.OFFER,
        "body_html": OFFER_CLIENT,
        "fields": _OFFER_FIELDS,
    },
    {
        "code": "OFFER_INTERNAL",
        "name": "Offer Letter (Internal)",
        "category": LetterCategory.OFFER,
        "body_html": OFFER_INTERNAL,
        "fields": _OFFER_FIELDS,
    },
    {
        "code": "PROMOTION",
        "name": "Promotion Letter",
        "category": LetterCategory.PROMOTION,
        "body_html": PROMOTION,
        "fields": [
            _f("computed.letter_date", "Letter date", C),
            _f("employee.full_name", "Full name", E),
            _f("employee.first_name", "First name", E),
            _f("employee.location", "Location", E),
            _f("manual.designation", "New designation", M, default="employee.designation"),
            _f("salary.effective_from", "Effective from (salary revision)", S),
            _f("salary.ctc_annual", "Revised CTC per year", S),
            _f("manual.notice_period", "Notice period", M, default="employee.notice_period"),
            _f("company.name", "Company name", T),
            _f("computed.salary_annexure", "Salary annexure", C),
            _f("computed.signature", "Signature", C, required=False),
        ],
    },
    {
        "code": "EXPERIENCE",
        "name": "Experience Letter",
        "category": LetterCategory.EXPERIENCE,
        "body_html": EXPERIENCE,
        "fields": [
            _f("computed.letter_date", "Letter date", C),
            _f("employee.salutation", "Mr. / Ms.", E),
            _f("employee.full_name", "Full name", E),
            _f("employee.first_name", "First name", E),
            _f("employee.employee_code", "Employee ID", E),
            _f("employee.date_of_joining", "Date of joining", E),
            _f("employee.date_of_exit", "Last working day", E),
            _f("employee.designation", "Designation", E),
            _f("company.name", "Company name", T),
            _f("computed.signature", "Signature", C, required=False),
        ],
    },
    {
        "code": "JOINING_FORM",
        "name": "Employee Joining Form",
        "category": LetterCategory.CUSTOM,
        "body_html": JOINING_FORM,
        # Only the name is required: every other line is left blank to be
        # written in by hand where the record does not know it yet.
        "fields": [_f("employee.full_name", "Full name", E)],
    },
]
