"""Where the data goes, and who else sees it — including model providers.

═══════════════════════════════════════════════════════════════════════════════
AN AGENT CALL IS A CROSS-BORDER TRANSFER
═══════════════════════════════════════════════════════════════════════════════

This is the connection almost everybody misses. A product sends an employee's
performance history to a model provider to draft an appraisal summary; that is
personal data leaving the country, processed by a third party, on a lawful basis
somebody has to be able to name. It does not stop being a transfer because it
was convenient, or because the response was not stored, or because the vendor
says they do not train on it.

So every AI call this product makes is a transfer against a registered
sub-processor, and the registry below is the same object the AI Trust Center
reads. There is no second list.

`categories_permitted` on each sub-processor is the hard edge: a category not in
that tuple must never be included in a payload sent there. The check is
`assert_transfer_allowed`, and it raises rather than filtering — a function that
quietly dropped the disallowed fields would let a caller keep sending them and
never find out.

**Slice 25 builds the register and the check. Nothing in the agent layer calls
it yet, because the agent layer is slices 27-29.** Stated here rather than
implied, because a compliance register that nothing enforces against is the
exact thing this file is written to replace.

Pure.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.services.dpdp.categories import CATEGORIES, category

__all__ = [
    "SUB_PROCESSORS",
    "ProcessingLocation",
    "SubProcessor",
    "TransferError",
    "TransferRecord",
    "assert_transfer_allowed",
    "crosses_border",
    "sub_processor",
    "trust_centre_disclosure",
]


class TransferError(ValueError):
    """A transfer this module will not let happen quietly."""


class ProcessingLocation(str, enum.Enum):
    """Where processing physically happens.

    DPDP s.16 lets the Central Government restrict transfers to notified
    countries. No such notification exists as this is written, so the practical
    obligation today is disclosure and contract, not blocking — and this enum
    exists so that when a notification does arrive, the answer to "which of our
    sub-processors are affected" is a filter rather than a project.
    """

    INDIA = "india"
    OUTSIDE_INDIA = "outside_india"
    #: Region not pinned by contract, so it has to be assumed to be outside.
    UNSPECIFIED = "unspecified"

    @property
    def is_cross_border(self) -> bool:
        # UNSPECIFIED counts as cross-border. A vendor who will not commit to a
        # region in writing has not committed to one, and a disclosure that
        # assumed otherwise would be a disclosure we could not stand behind.
        return self is not ProcessingLocation.INDIA


@dataclass(frozen=True, slots=True)
class SubProcessor:
    """One third party that processes personal data on our behalf."""

    key: str
    name: str
    #: What they do for us, in words a tenant's DPO can evaluate.
    service: str
    location: ProcessingLocation
    country: str
    #: Category keys they may receive. Enforced, not documented.
    categories_permitted: tuple[str, ...]
    #: True for a model provider. Called out separately because the AI Trust
    #: Center has to list these specifically, and because the failure mode —
    #: data in a prompt — looks nothing like a database replica.
    is_ai_model_provider: bool = False
    #: What contractually constrains them. Standard contractual clauses, a DPA,
    #: a zero-retention commitment.
    safeguards: tuple[str, ...] = ()
    #: Whether the tenant can decline this sub-processor and still use the
    #: product. False for the ones the product cannot run without.
    is_optional: bool = False

    @property
    def is_cross_border(self) -> bool:
        return self.location.is_cross_border

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "service": self.service,
            "location": self.location.value,
            "country": self.country,
            "is_cross_border": self.is_cross_border,
            "is_ai_model_provider": self.is_ai_model_provider,
            "is_optional": self.is_optional,
            "categories_permitted": list(self.categories_permitted),
            "category_names": [
                CATEGORIES[key].name
                for key in self.categories_permitted
                if key in CATEGORIES
            ],
            "safeguards": list(self.safeguards),
        }


def _s(**kwargs: Any) -> SubProcessor:
    return SubProcessor(**kwargs)


#: Every third party that receives personal data from this product.
#:
#: The list is short and it should stay short. Each entry is a place a tenant's
#: employees' data goes, and a register that grows without anybody noticing is
#: how a DPO ends up learning about a sub-processor from a news article.
SUB_PROCESSORS: dict[str, SubProcessor] = {
    s.key: s
    for s in (
        _s(
            key="supabase",
            name="Supabase",
            service=(
                "Managed Postgres. Holds the primary database — every category "
                "of personal data this product stores."
            ),
            location=ProcessingLocation.INDIA,
            country="India (ap-south-1, Mumbai)",
            categories_permitted=tuple(CATEGORIES),
            safeguards=(
                "Data processing agreement",
                "Region pinned to ap-south-1; no cross-region replication enabled",
                "Encryption at rest and in transit",
            ),
        ),
        _s(
            key="anthropic",
            name="Anthropic",
            service=(
                "The model behind the AI assistant and culture sentiment "
                "analysis. Receives whatever context a query needs, in a prompt."
            ),
            location=ProcessingLocation.OUTSIDE_INDIA,
            country="United States",
            categories_permitted=(
                "ai_interaction",
                "culture_response",
                "recruitment",
                "organisational",
            ),
            is_ai_model_provider=True,
            is_optional=True,
            safeguards=(
                "Zero data retention configured on the API account",
                "Standard contractual clauses",
                "Prompts carry no statutory identifier, bank detail or health "
                "category — enforced by assert_transfer_allowed, not by policy",
            ),
        ),
        _s(
            key="resend",
            name="Resend",
            service="Transactional email delivery.",
            location=ProcessingLocation.OUTSIDE_INDIA,
            country="United States",
            categories_permitted=("identity", "communication"),
            safeguards=(
                "Data processing agreement",
                "Receives a name, an email address and the message body only",
            ),
        ),
        _s(
            key="meta_whatsapp",
            name="Meta (WhatsApp Business Platform)",
            service="WhatsApp message delivery for notifications.",
            location=ProcessingLocation.OUTSIDE_INDIA,
            country="United States / Ireland",
            categories_permitted=("identity", "communication"),
            is_optional=True,
            safeguards=(
                "Data processing agreement",
                "Receives a phone number and a pre-approved template's variables",
            ),
        ),
    )
}


def sub_processor(key: str) -> SubProcessor:
    try:
        return SUB_PROCESSORS[key]
    except KeyError:
        raise KeyError(
            f"No sub-processor {key!r}. Registered: "
            f"{', '.join(sorted(SUB_PROCESSORS))}."
        ) from None


def crosses_border(key: str) -> bool:
    return sub_processor(key).is_cross_border


def assert_transfer_allowed(
    sub_processor_key: str, category_keys: tuple[str, ...]
) -> None:
    """Refuse a transfer of categories a sub-processor may not receive.

    Raises rather than filtering. A version of this that dropped the offending
    categories and carried on would let a caller keep sending an Aadhaar number
    to a model provider indefinitely and never discover it — the payload would
    just be quietly smaller than the caller built.
    """
    spec = sub_processor(sub_processor_key)

    unknown = [key for key in category_keys if key not in CATEGORIES]
    if unknown:
        raise TransferError(
            f"Cannot check a transfer of {', '.join(sorted(unknown))} — not in "
            "the category catalogue. An unclassified payload cannot be cleared "
            "to leave."
        )

    disallowed = [
        key for key in category_keys if key not in spec.categories_permitted
    ]
    if disallowed:
        names = ", ".join(sorted(category(k).name for k in disallowed))
        border = (
            f"This is a cross-border transfer to {spec.country}. "
            if spec.is_cross_border
            else ""
        )
        raise TransferError(
            f"{spec.name} may not receive: {names}. {border}"
            "Remove these categories from the payload rather than widening the "
            "sub-processor's permission — the permitted list is what the tenant "
            "was told in the Trust Center."
        )


@dataclass(frozen=True, slots=True)
class TransferRecord:
    """One transfer that happened. Insert-only.

    Recorded per call, not per configuration. "Which of our employees' data has
    been sent to a model provider, and when" is a question a tenant will ask,
    and answering it from a configuration screen — "the AI assistant is enabled"
    — is not answering it.
    """

    sub_processor_key: str
    category_keys: tuple[str, ...]
    principal_count: int
    occurred_at: datetime
    purpose_key: str
    #: What was done, in a line. "Drafted an appraisal summary for one employee."
    description: str

    @property
    def is_cross_border(self) -> bool:
        return crosses_border(self.sub_processor_key)

    def as_dict(self) -> dict[str, Any]:
        spec = sub_processor(self.sub_processor_key)
        return {
            "sub_processor": self.sub_processor_key,
            "sub_processor_name": spec.name,
            "country": spec.country,
            "is_cross_border": self.is_cross_border,
            "is_ai_model_provider": spec.is_ai_model_provider,
            "categories": list(self.category_keys),
            "principal_count": self.principal_count,
            "occurred_at": self.occurred_at.isoformat(),
            "purpose": self.purpose_key,
            "description": self.description,
        }


def trust_centre_disclosure() -> dict[str, Any]:
    """What the AI Trust Center shows a tenant about where their data goes.

    Model providers are listed first and separately. Everything else on this
    page a buyer half expects — a database host, an email relay. A model
    provider seeing employee data is the one they have not thought about, and
    burying it in an alphabetical list of vendors would be a disclosure
    technically made and practically not.
    """
    ai = [s for s in SUB_PROCESSORS.values() if s.is_ai_model_provider]
    others = [s for s in SUB_PROCESSORS.values() if not s.is_ai_model_provider]

    return {
        "ai_model_providers": [s.as_dict() for s in ai],
        "other_sub_processors": [s.as_dict() for s in others],
        "cross_border_count": sum(
            1 for s in SUB_PROCESSORS.values() if s.is_cross_border
        ),
        "in_india_count": sum(
            1 for s in SUB_PROCESSORS.values() if not s.is_cross_border
        ),
        "statement": (
            "Your employees' primary records are stored in India. The "
            f"{len(ai)} AI model provider(s) listed above process data outside "
            "India when your users use AI features; those features are optional "
            "and can be switched off per tenant. The categories each provider "
            "may receive are enforced in code, not only in contract — a payload "
            "containing a category not listed is refused before it is sent."
        ),
        "not_yet_wired": (
            "The AI feature layer is not built as of this release, so no "
            "transfer to a model provider has occurred. This register describes "
            "what will be permitted when it is."
        ),
    }
