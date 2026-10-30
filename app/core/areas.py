"""Permission areas: what an HR operator's rights are granted in.

A sub-admin is HR — they act on anybody's record — unless an admin narrows
that. The narrowing is by area (payroll, hiring, ...) and by right (view,
create, edit, delete, approve, export), from `RoleModulePermission` for the
role and `UserAreaRights` for one person, the person's row winning. Where a
right is withheld, the sub-admin is not HR for that request: their own
self-service and any line-manager rights still work, the HR action does not.

Which area a request is in comes from its path, and which right it needs from
its method and the shape of the path. Routes outside every area — account,
settings, privacy, security — are not narrowed here: they have their own gates.

Pure.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "AREAS",
    "RIGHTS",
    "AreaRights",
    "area_for_path",
    "required_right",
]

#: The areas, with what each one covers, as an admin reads them.
AREAS: dict[str, str] = {
    "people": "Employee records, documents, letters, onboarding, exits, assets, helpdesk",
    "time": "Leave, attendance and overtime",
    "payroll": "Payroll runs, salary, tax, statutory filings, bank files, loans, gratuity",
    "expenses": "Expense and travel claims",
    "hiring": "Requisitions, candidates, interviews, offers and job boards",
    "performance": "Appraisals, calibration, goals, feedback and one-on-ones",
    "culture": "Surveys, recognition and rewards",
    "analytics": "Reports, people analytics and the BI connector",
}

RIGHTS: tuple[str, ...] = (
    "can_view",
    "can_create",
    "can_edit",
    "can_delete",
    "can_approve",
    "can_export",
)

_SEGMENT_AREA: dict[str, str] = {
    **dict.fromkeys(
        (
            "employees",
            "org",
            "positions",
            "lifecycle",
            "exits",
            "documents",
            "letters",
            "letter-templates",
            "onboarding",
            "assets",
            "helpdesk",
        ),
        "people",
    ),
    **dict.fromkeys(("leave", "attendance"), "time"),
    **dict.fromkeys(
        (
            "payroll",
            "salaries",
            "salary-components",
            "salary-structures",
            "tax",
            "statutory",
            "filings",
            "disbursement",
            "arrears",
            "loans",
            "reimbursements",
            "gratuity",
            "encashment",
            "gst",
            "accounts",
        ),
        "payroll",
    ),
    "expenses": "expenses",
    **dict.fromkeys(
        ("hiring", "offers", "careers", "interviews", "recruiting", "job-boards"), "hiring"
    ),
    **dict.fromkeys(
        (
            "appraisal",
            "calibration",
            "competency",
            "cycle-config",
            "performance",
            "goals",
            "feedback",
            "one-on-ones",
        ),
        "performance",
    ),
    **dict.fromkeys(("culture", "rewards"), "culture"),
    **dict.fromkeys(("reports", "bi"), "analytics"),
}

_APPROVE_WORDS = frozenset(
    {"approve", "decide", "decision", "release", "reject", "attest", "sign-off", "finalise"}
)
_EXPORT_WORDS = ("export", "download", ".csv", "journal", "register", "/file", "ecr", "24q")


@dataclass(frozen=True, slots=True)
class AreaRights:
    can_view: bool = True
    can_create: bool = True
    can_edit: bool = True
    can_delete: bool = True
    can_approve: bool = True
    can_export: bool = True

    def allows(self, right: str) -> bool:
        return bool(getattr(self, right))


def area_for_path(path: str) -> str | None:
    """The area a request path is in, or None for one outside every area."""
    trimmed = path.split("?", 1)[0]
    if "/api/v1/" in trimmed:
        trimmed = trimmed.split("/api/v1/", 1)[1]
    first = trimmed.strip("/").split("/", 1)[0]
    if first == "public":
        return None
    return _SEGMENT_AREA.get(first)


def required_right(method: str, path: str) -> str:
    """The right a request needs: approving and exporting are named apart from writing."""
    lowered = path.lower()
    segments = set(lowered.strip("/").split("/"))
    if segments & _APPROVE_WORDS:
        return "can_approve"
    if method.upper() in ("GET", "HEAD") and any(word in lowered for word in _EXPORT_WORDS):
        return "can_export"
    # A file built from a list too long for a URL (the people ticked on screen)
    # is still a download: it writes nothing but the audit log.
    if method.upper() == "POST" and lowered.split("?", 1)[0].rstrip("/").endswith("/export"):
        return "can_export"
    return {
        "GET": "can_view",
        "HEAD": "can_view",
        "POST": "can_create",
        "PUT": "can_edit",
        "PATCH": "can_edit",
        "DELETE": "can_delete",
    }.get(method.upper(), "can_view")
