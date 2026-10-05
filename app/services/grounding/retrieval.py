"""One retrieval path, permission-scoped, with sources no agent can reach.

═══════════════════════════════════════════════════════════════════════════════
SCOPING AT RETRIEVAL, NOT AT OUTPUT
═══════════════════════════════════════════════════════════════════════════════

The tempting shortcut is to retrieve broadly and filter the answer. It looks
equivalent and is not:

- The model has seen the data. A prompt-injection attempt, a follow-up question,
  or a summary that "does not name anybody" can all leak what was retrieved.
- The retrieval log — which slice 30 publishes — then records that a manager's
  query pulled another team's records, and no amount of output filtering makes
  that untrue.
- Any bug in the output filter is a disclosure. Any bug in the retrieval scope
  is a missing answer.

Those failure modes are not comparable, so the scope is applied to the query.
`scope_for` is the single place it is decided, and an invariant checks no agent
path builds a query without it.

═══════════════════════════════════════════════════════════════════════════════
YOUR VOICE IS NOT RETRIEVABLE, BY ANY AGENT, EVER
═══════════════════════════════════════════════════════════════════════════════

The whole value of a confidential reporting channel is that it is closed. An
agent surfacing its content anywhere — in a summary, as a citation, as a signal
feeding a retention score — breaks the promise the module makes, and it breaks
it for every future reporter, not just the one whose case leaked.

So `FORBIDDEN_SOURCES` is checked in `assert_retrievable`, which raises. There is
no permission level that unlocks it and no parameter that overrides it: a
super admin debugging a support ticket must not be able to ask an agent about
a Your Voice case either.

**The Your Voice module is not built yet** (it is a later slice). The exclusion
is written now, with its table names declared in advance, because the failure
mode is a module arriving later and nobody remembering to add it. An invariant
checks the forbidden list is non-empty and contains the Your Voice tables.

Pure. The actual query lives in `service.py`.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "FORBIDDEN_SOURCES",
    "RetrievalError",
    "RetrievalScope",
    "RetrievedRecord",
    "ScopeKind",
    "assert_retrievable",
    "scope_for",
]


class RetrievalError(ValueError):
    """A retrieval this module refuses to perform."""


class ScopeKind(str, enum.Enum):
    """How wide a requester's grounding reach is.

    Deliberately coarse. A finer model would be more accurate and would also be
    something nobody can hold in their head while reviewing an agent, and the
    property that matters here is that a reviewer can say what a given user can
    reach without tracing code.
    """

    #: Their own records only. Every employee has at least this.
    SELF = "self"
    #: Themselves plus their direct and indirect reports.
    TEAM = "team"
    #: The whole tenant's HR data, minus the forbidden sources.
    TENANT = "tenant"

    @property
    def rank(self) -> int:
        return {ScopeKind.SELF: 0, ScopeKind.TEAM: 1, ScopeKind.TENANT: 2}[self]


#: Tables no agent may ever retrieve from, whatever the requester's role.
#:
#: `your_voice_*` are declared before the module exists. That is the point: a
#: forbidden list written after the module ships is a list somebody has to
#: remember to update, and this one cannot be forgotten because the check is
#: already running.
FORBIDDEN_SOURCES: frozenset[str] = frozenset(
    {
        # Confidential reporting. The entire value rests on being closed.
        "your_voice_cases",
        "your_voice_messages",
        "your_voice_case_events",
        # POSH complaints and disciplinary enquiries. Same argument: a person
        # named in one did not consent to it becoming an input to a summary
        # about somebody else.
        "posh_cases",
        "disciplinary_records",
        # Credentials and session material. Never grounding content, and an
        # agent that can read them is an agent that can be made to print them.
        "users",
        "scim_tokens",
        "candidate_portal_tokens",
        "self_schedule_links",
        "identity_providers",
        "saml_assertions_seen",
        # Somebody else's exercised privacy rights.
        "data_principal_requests",
        "consent_records",
    }
)

#: Sources that carry health or health-adjacent content. Retrievable by HR for
#: a factual question about the requester themselves, and never aggregated into
#: an inference about a person — enforced in `sensitive.py`, listed here so the
#: two modules read the same names.
HEALTH_ADJACENT_SOURCES: frozenset[str] = frozenset(
    {"leave_requests", "esi_coverage_periods", "employee_documents"}
)


@dataclass(frozen=True, slots=True)
class RetrievalScope:
    """What one requester may ground an answer in.

    `employee_ids` is the resolved set, not a rule to be applied later. A scope
    that says "their team" and leaves the expansion to the query is a scope that
    two call sites will expand differently.
    """

    kind: ScopeKind
    requester_employee_id: str | None
    #: The resolved set of employees in reach. Empty with `TENANT` means the
    #: whole tenant; empty with `SELF` or `TEAM` means nobody, which is a real
    #: state — a manager with no reports has a team of themselves.
    employee_ids: frozenset[str] = frozenset()
    tenant_id: str = ""

    def covers(self, employee_id: str) -> bool:
        if self.kind is ScopeKind.TENANT:
            return True
        return employee_id in self.employee_ids

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "requester_employee_id": self.requester_employee_id,
            "employee_count": (
                "all" if self.kind is ScopeKind.TENANT else len(self.employee_ids)
            ),
        }


#: Which coarse role gets which scope. One table, so "what can a manager reach"
#: is answerable by reading four lines rather than by tracing a query builder.
#: `manager` is an employee with a direct report (`permissions.scope_role`).
_ROLE_SCOPE: dict[str, ScopeKind] = {
    "super_admin": ScopeKind.TENANT,
    "admin": ScopeKind.TENANT,
    "sub_admin": ScopeKind.TENANT,
    "manager": ScopeKind.TEAM,
    "employee": ScopeKind.SELF,
}


def scope_for(
    *,
    role: str,
    tenant_id: str,
    requester_employee_id: str | None,
    reachable_employee_ids: frozenset[str] = frozenset(),
) -> RetrievalScope:
    """The single place a requester's grounding reach is decided.

    An unknown role gets `SELF`, not `TENANT`. A new role added to the enum and
    forgotten here should be able to ground an answer in its own record and
    nothing else — defaulting the other way would make every future role a
    tenant-wide reader until somebody noticed.
    """
    kind = _ROLE_SCOPE.get(role, ScopeKind.SELF)

    if kind is ScopeKind.SELF:
        resolved = (
            frozenset({requester_employee_id}) if requester_employee_id else frozenset()
        )
    elif kind is ScopeKind.TEAM:
        resolved = frozenset(reachable_employee_ids)
        if requester_employee_id:
            resolved = resolved | {requester_employee_id}
    else:
        resolved = frozenset()

    return RetrievalScope(
        kind=kind,
        requester_employee_id=requester_employee_id,
        employee_ids=resolved,
        tenant_id=tenant_id,
    )


def assert_retrievable(source: str) -> None:
    """Refuse a forbidden source. No role unlocks it, no flag overrides it.

    Raises rather than returning a boolean. A boolean invites `if allowed:` and
    the branch somebody forgets; a raise stops the call.
    """
    if source in FORBIDDEN_SOURCES:
        raise RetrievalError(
            f"{source!r} is not retrievable by any agent, for any requester. "
            "Confidential reporting, disciplinary matters and credentials are "
            "outside every agent's reach — there is no role that unlocks this "
            "and no parameter that overrides it. If an agent needs to answer a "
            "question about one of these, the answer is that a person does."
        )


@dataclass(frozen=True, slots=True)
class RetrievedRecord:
    """One record pulled for grounding, with the identifier that travels on.

    `citation_id` is what appears in the prompt and has to come back in the
    model's citation. Constructed here rather than by the caller so the format
    is the same everywhere and `citations.py` can parse it without a convention
    somebody has to remember.
    """

    source: str
    record_id: str
    content: dict[str, Any]
    #: The employee this record is about, where it is about one. Used to check
    #: the record was inside the scope that retrieved it — a second check, on
    #: the way out, against a query bug on the way in.
    subject_employee_id: str | None = None

    @property
    def citation_id(self) -> str:
        return f"{self.source}:{self.record_id}"

    def as_prompt_block(self) -> str:
        """How this record appears in a prompt.

        Labelled and delimited. The identifier is inside the block so a model
        that quotes the content has the citation to hand, and outside the
        user-content delimiters used in `injection.py` so retrieved records and
        employee-typed text are never the same kind of thing in the prompt.
        """
        lines = [f"<record id=\"{self.citation_id}\">"]
        lines.extend(f"  {key}: {value}" for key, value in self.content.items())
        lines.append("</record>")
        return "\n".join(lines)

    def as_dict(self) -> dict[str, Any]:
        return {
            "citation_id": self.citation_id,
            "source": self.source,
            "record_id": self.record_id,
            "subject_employee_id": self.subject_employee_id,
        }


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    """Everything one retrieval produced, and what it refused.

    `refused_sources` is returned rather than silently dropped. An agent asked
    a question it could not ground because a source was out of reach should be
    able to say so — "I do not have access to that" is a better answer than a
    confident one built on what was left.
    """

    records: tuple[RetrievedRecord, ...]
    scope: RetrievalScope
    refused_sources: tuple[str, ...] = ()
    #: Records dropped because they fell outside the scope. Should always be
    #: empty: a non-empty value means the query was wider than the scope and is
    #: a bug, so it is surfaced rather than quietly filtered.
    dropped_out_of_scope: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def citation_ids(self) -> frozenset[str]:
        return frozenset(r.citation_id for r in self.records)

    def as_dict(self) -> dict[str, Any]:
        return {
            "record_count": len(self.records),
            "citation_ids": sorted(self.citation_ids),
            "scope": self.scope.as_dict(),
            "refused_sources": list(self.refused_sources),
            "dropped_out_of_scope": list(self.dropped_out_of_scope),
            "notes": list(self.notes),
        }


def enforce_scope(
    records: list[RetrievedRecord], scope: RetrievalScope
) -> tuple[list[RetrievedRecord], list[str]]:
    """Drop anything outside the scope, and say what was dropped.

    This is a **second** check. The query is supposed to be scoped already, and
    `dropped_out_of_scope` being non-empty means it was not — which is a bug
    worth surfacing loudly rather than a filter quietly doing its job. The
    invariant that matters is the one on the query; this is the net under it.
    """
    kept: list[RetrievedRecord] = []
    dropped: list[str] = []
    for record in records:
        if record.subject_employee_id is None:
            kept.append(record)
            continue
        if scope.covers(record.subject_employee_id):
            kept.append(record)
        else:
            dropped.append(record.citation_id)
    return kept, dropped
