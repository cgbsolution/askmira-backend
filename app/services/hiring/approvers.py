"""Who may sign a step in a requisition or offer approval chain.

A chain step names a role — `hiring_manager`, `tenant_admin`, sometimes a label
like `finance_head` that this product has no user role for. Until now the route
let any manager or HR user decide any step, which made the chain a list of
names rather than a list of signatures.

Two rules, stated once:

- **Nobody decides their own.** The person who raised a requisition or drafted
  an offer never approves it, whatever their role. That is the entire value of
  a second signature.
- **The step's role decides who may sign it.** An admin role can sign for the
  roles beneath it; a label with no matching user role is signed by a tenant or
  HR admin on its behalf, and the response says so rather than pretending the
  named person signed.

Pure.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["StepAuthority", "can_decide_step"]

_HR = frozenset({"tenant_admin", "hr_admin", "hr_ops"})
_ADMIN = frozenset({"tenant_admin", "hr_admin"})
_KNOWN = frozenset({"hiring_manager", "manager", "hr_ops", "hr_admin", "tenant_admin"})


@dataclass(frozen=True, slots=True)
class StepAuthority:
    allowed: bool
    #: Set when an admin signs for a role the product has no users for.
    on_behalf_of: str | None
    message: str


def can_decide_step(
    *,
    approver_role: str,
    user_role: str,
    is_hiring_manager: bool,
    is_originator: bool,
) -> StepAuthority:
    """Whether this person may sign this step."""
    if is_originator:
        return StepAuthority(
            allowed=False,
            on_behalf_of=None,
            message=(
                "You raised this, so you cannot approve it. A second signature is only "
                "worth something if it belongs to somebody else."
            ),
        )

    step = (approver_role or "").strip().lower()
    role = (user_role or "").strip().lower()
    if role == "platform_admin":
        return StepAuthority(True, None, "")

    if step == "hiring_manager":
        allowed = role in _HR or (is_hiring_manager and role == "manager")
        needed = "the requisition's hiring manager or HR"
    elif step == "manager":
        allowed = role in _HR or role == "manager"
        needed = "a manager or HR"
    elif step == "hr_ops":
        allowed = role in _HR
        needed = "HR"
    elif step == "hr_admin":
        allowed = role in _ADMIN
        needed = "an HR admin or the tenant admin"
    elif step == "tenant_admin":
        allowed = role == "tenant_admin"
        needed = "the tenant admin"
    else:
        allowed = role in _ADMIN
        needed = "a tenant or HR admin, signing on behalf of this role"
        if allowed:
            return StepAuthority(
                allowed=True,
                on_behalf_of=approver_role,
                message=(
                    f"'{approver_role}' is not a role anybody holds in AskMira, so this "
                    "step is signed by an admin on its behalf. The approval record says so."
                ),
            )

    if allowed:
        return StepAuthority(True, None, "")
    return StepAuthority(
        allowed=False,
        on_behalf_of=None,
        message=(
            f"This step needs {needed}. Your role cannot sign it, so nothing was recorded."
        ),
    )


def is_known_role(approver_role: str) -> bool:
    return (approver_role or "").strip().lower() in _KNOWN
