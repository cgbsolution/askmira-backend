"""Every signed-in endpoint with no role gate, and the rule that makes it safe.

An endpoint without `require_roles` is reachable by every signed-in person in
the tenant. That is right for a great many of them — a holiday calendar, your
own notifications, your own tax declaration. It stopped being right, quietly,
for about thirty endpoints that took an `employee_id` or a record id from the
request and trusted it: a colleague's leave reason, their offer letter, their
reward points.

So every ungated endpoint is declared with its rule, and invariants check both
halves: nothing ungated is missing from the declaration, and each rule is really
present in the code it claims to describe. A new endpoint cannot ship ungated by
omission; it has to be written down with a reason somebody can argue with.

The declarations live one module per file in `app.core.access_policies`, and are
merged here. A key declared in two files is an import-time error.

Rules, as the prefix of each entry:

- ``own``         — acts only on the caller's own identity, taken from the token.
- ``reference``   — tenant-wide or static material about nobody in particular.
                    It takes no person id, and the invariant checks that it does not.
- ``permissions`` — decides self / reporting line / HR through `app.core.permissions`.
- ``scoped:<marker>`` — a different enforced check, named by a marker that must
                    appear in the handler or the service it calls.
"""

from __future__ import annotations

from app.core.access_policies import (
    culture,
    documents,
    expenses,
    hiring,
    identity,
    payroll,
    payroll_run,
    people,
    performance,
    platform,
    rewards,
    time,
)

__all__ = ["RULES", "UNGATED_ENDPOINTS"]

RULES = ("own", "reference", "permissions", "scoped")

_MODULES = (
    identity,
    people,
    time,
    payroll,
    payroll_run,
    documents,
    hiring,
    culture,
    expenses,
    performance,
    rewards,
    platform,
)


def _merge() -> dict[str, str]:
    merged: dict[str, str] = {}
    for module in _MODULES:
        for key, rule in module.ENDPOINTS.items():
            if key in merged:
                raise RuntimeError(
                    f"{key} is declared in more than one access policy module "
                    f"(again in {module.__name__}). Declare it once."
                )
            merged[key] = rule
    return merged


UNGATED_ENDPOINTS: dict[str, str] = _merge()
