"""Whether an input credit is claimable, and — where unclear — saying so.

═══════════════════════════════════════════════════════════════════════════════
UNCERTAIN IS A THIRD ANSWER, NOT A ROUNDED-DOWN ONE
═══════════════════════════════════════════════════════════════════════════════

Two failure modes, both expensive and both invisible:

- Guess **eligible** and the company claims credit it is not entitled to. That
  reverses with interest, and it reverses years later during an assessment.
- Guess **ineligible** and the company quietly leaks money — which is the exact
  problem this slice exists to fix, arriving by a different door.

So `UNCERTAIN` is a real verdict that routes to a finance review queue with the
reason written out. A line nobody could decide is a line a person decides.

═══════════════════════════════════════════════════════════════════════════════
THE PLACE-OF-SUPPLY TRAP IS WHERE THE REAL MONEY IS
═══════════════════════════════════════════════════════════════════════════════

A Karnataka-registered company sends somebody to Delhi. The hotel is an
intra-state supply *in Delhi* and charges CGST+SGST of Delhi. The company is not
registered in Delhi, so it cannot take that credit — and almost every
spreadsheet-based capture claims it anyway, because the invoice says "GST" and
the amount is right there.

`determine_tax_split` and `assess` model this explicitly rather than adding up
whatever tax columns the invoice happens to carry.

═══════════════════════════════════════════════════════════════════════════════
AND EVERY RULE HERE AWAITS A TAX ADVISOR
═══════════════════════════════════════════════════════════════════════════════

`DEFAULT_BLOCKED_CATEGORIES` reflects a reading of Section 17(5) and carries
`reviewed=False`. The exceptions are the hard part — a motor vehicle is blocked
except when it is used for further supply of vehicles, or transport of
passengers, or driving instruction; food and beverage is blocked except where an
employer is obliged to provide it under another law. None of that is decidable
from an expense category, so those cases return `UNCERTAIN` rather than a
confident answer.

Pure.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

__all__ = [
    "DEFAULT_BLOCKED_CATEGORIES",
    "BlockedCategoryRule",
    "Eligibility",
    "EligibilityError",
    "InvoiceFacts",
    "TaxSplit",
    "assess",
    "determine_tax_split",
]


class EligibilityError(ValueError):
    """An eligibility determination this module refuses to make."""


def money(value: Decimal | int | str) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class Eligibility(str, enum.Enum):
    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"
    #: Nobody could decide from what is on the invoice. Goes to a person.
    UNCERTAIN = "uncertain"

    @property
    def needs_review(self) -> bool:
        return self is Eligibility.UNCERTAIN


class TaxSplit(str, enum.Enum):
    """Which taxes an invoice should carry, given where it was supplied."""

    #: Supplier and place of supply in the recipient's state.
    CGST_SGST = "cgst_sgst"
    #: Inter-state, or an import.
    IGST = "igst"
    #: Intra-state in a state the recipient is not registered in. The tax is
    #: real and the credit is not available.
    CGST_SGST_OTHER_STATE = "cgst_sgst_other_state"


@dataclass(frozen=True, slots=True)
class BlockedCategoryRule:
    """One category blocked or restricted under Section 17(5).

    `has_exceptions` is the important field. Where the section allows the credit
    in specific circumstances that an expense category cannot express, the
    verdict is `UNCERTAIN` and a person decides — because a blanket "ineligible"
    on motor vehicles leaks credit for a logistics company, and a blanket
    "eligible" claims credit nobody is entitled to.
    """

    category_key: str
    reason: str
    #: Statutory reference, so a reviewer can check the reading rather than
    #: only the outcome.
    reference: str = "Section 17(5), CGST Act"
    has_exceptions: bool = False
    exception_note: str = ""
    reviewed: bool = False
    reviewed_by: str = ""

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise EligibilityError(
                f"The rule for {self.category_key!r} has no reason. A line "
                "marked ineligible with no explanation is one finance cannot "
                "challenge and will eventually stop trusting."
            )
        if self.reviewed and not self.reviewed_by.strip():
            raise EligibilityError("A rule marked reviewed has to name who reviewed it.")


#: A reading of Section 17(5), every entry unreviewed. These are a starting
#: point for a conversation with the tenant's advisor, not an answer.
DEFAULT_BLOCKED_CATEGORIES: tuple[BlockedCategoryRule, ...] = (
    BlockedCategoryRule(
        category_key="food_and_beverage",
        reason=("Food and beverages are among the supplies on which input credit is blocked."),
        has_exceptions=True,
        exception_note=(
            "The block lifts where the supply is used to make an outward supply "
            "of the same category, or where an employer is obliged to provide "
            "it under another law in force. Neither is decidable from an "
            "expense category, so this routes to review."
        ),
    ),
    BlockedCategoryRule(
        category_key="outdoor_catering",
        reason="Outdoor catering is blocked on the same footing as food.",
        has_exceptions=True,
        exception_note=(
            "Same exceptions as food and beverage; an event catered as part of "
            "an onward supply is a different case."
        ),
    ),
    BlockedCategoryRule(
        category_key="motor_vehicle",
        reason=(
            "Motor vehicles for the transport of persons with limited seating "
            "capacity are blocked, along with their servicing and insurance."
        ),
        has_exceptions=True,
        exception_note=(
            "The block does not apply where the vehicle is used for further "
            "supply of vehicles, transport of passengers, or driving "
            "instruction. A logistics or fleet business is exactly the case a "
            "blanket 'ineligible' would cost money on."
        ),
    ),
    BlockedCategoryRule(
        category_key="rent_a_cab",
        reason="Renting of motor vehicles is in the blocked list.",
        has_exceptions=True,
        exception_note=(
            "Exceptions turn on the same use tests as motor vehicles and on an "
            "obligation under another law."
        ),
    ),
    BlockedCategoryRule(
        category_key="health_insurance",
        reason=("Health insurance and health services for employees are in the blocked list."),
        has_exceptions=True,
        exception_note=(
            "The block lifts where the employer is obliged to provide the cover "
            "under a law in force - which is a genuinely common case in India "
            "and cannot be read off an expense category."
        ),
    ),
    BlockedCategoryRule(
        category_key="club_membership",
        reason=(
            "Membership of a club, health or fitness centre is blocked with no "
            "employee-benefit exception."
        ),
        has_exceptions=False,
    ),
    BlockedCategoryRule(
        category_key="employee_travel_benefit",
        reason=(
            "Travel benefits extended to employees on vacation, such as leave "
            "or home travel concession, are blocked."
        ),
        has_exceptions=False,
        exception_note=(
            "Business travel is a different thing entirely and is not covered by this rule."
        ),
    ),
    BlockedCategoryRule(
        category_key="works_contract_immovable",
        reason=(
            "Works contract services for construction of immovable property are "
            "blocked other than for plant and machinery."
        ),
        has_exceptions=True,
        exception_note=(
            "Plant and machinery is carved out, and whether a given fit-out "
            "falls inside it is a question for an advisor."
        ),
    ),
    BlockedCategoryRule(
        category_key="personal_consumption",
        reason="Goods or services used for personal consumption are blocked.",
        has_exceptions=False,
    ),
)


def determine_tax_split(
    *,
    supplier_state_code: str,
    place_of_supply_state_code: str,
    recipient_state_code: str,
) -> TaxSplit:
    """Which taxes should be on the invoice, and whether we can use them.

    The third case is the one that costs money. An intra-state supply in a state
    the recipient is not registered in carries perfectly real CGST and SGST that
    the recipient simply cannot claim - and the invoice looks exactly like a
    claimable one.
    """
    for label, code in (
        ("supplier", supplier_state_code),
        ("place of supply", place_of_supply_state_code),
        ("recipient", recipient_state_code),
    ):
        if not code or len(code) != 2 or not code.isdigit():
            raise EligibilityError(
                f"The {label} state code {code!r} is not a two-digit GST state "
                "code, so the tax split cannot be determined. Guessing it is "
                "how a Delhi hotel bill gets claimed by a Karnataka entity."
            )

    if supplier_state_code != place_of_supply_state_code:
        return TaxSplit.IGST
    if place_of_supply_state_code == recipient_state_code:
        return TaxSplit.CGST_SGST
    return TaxSplit.CGST_SGST_OTHER_STATE


@dataclass(frozen=True, slots=True)
class InvoiceFacts:
    """One captured invoice line, as extracted."""

    category_key: str
    vendor_gstin: str
    invoice_number: str
    taxable_value: Decimal
    cgst: Decimal = Decimal("0")
    sgst: Decimal = Decimal("0")
    igst: Decimal = Decimal("0")
    cess: Decimal = Decimal("0")
    supplier_state_code: str = ""
    place_of_supply_state_code: str = ""
    recipient_state_code: str = ""
    hsn_sac: str = ""
    gstin_is_valid: bool = True

    @property
    def total_tax(self) -> Decimal:
        return money(self.cgst + self.sgst + self.igst + self.cess)


@dataclass(frozen=True, slots=True)
class EligibilityVerdict:
    """What is claimable on one line, and why.

    `claimable_amount` is zero for anything that is not `ELIGIBLE`, including
    `UNCERTAIN` — a figure nobody has decided must not appear in a total that
    finance reads as claimable. `amount_at_stake` carries it separately so the
    review queue can be prioritised by money.
    """

    eligibility: Eligibility
    reason: str
    reference: str
    tax_split: TaxSplit | None
    claimable_amount: Decimal
    amount_at_stake: Decimal
    needs_review: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "eligibility": self.eligibility.value,
            "reason": self.reason,
            "reference": self.reference,
            "tax_split": self.tax_split.value if self.tax_split else None,
            "claimable_amount": str(self.claimable_amount),
            "amount_at_stake": str(self.amount_at_stake),
            "needs_review": self.needs_review,
        }


def assess(
    invoice: InvoiceFacts,
    *,
    blocked: tuple[BlockedCategoryRule, ...] = DEFAULT_BLOCKED_CATEGORIES,
) -> EligibilityVerdict:
    """Decide one line, or say honestly that it cannot be decided here.

    Order matters. The GSTIN is checked first because without a valid one there
    is no credit whatever the category says; the blocked list is checked before
    the place of supply because a blocked category is blocked wherever it was
    supplied.
    """
    tax = invoice.total_tax

    if not invoice.gstin_is_valid:
        return EligibilityVerdict(
            eligibility=Eligibility.INELIGIBLE,
            reason=(
                "The vendor GSTIN did not validate, so there is no registration "
                "to claim the credit against. The expense itself is unaffected."
            ),
            reference="Structural validation",
            tax_split=None,
            claimable_amount=Decimal("0.00"),
            amount_at_stake=tax,
            needs_review=False,
        )

    rule = next((r for r in blocked if r.category_key == invoice.category_key), None)
    if rule is not None:
        if rule.has_exceptions:
            return EligibilityVerdict(
                eligibility=Eligibility.UNCERTAIN,
                reason=(
                    f"{rule.reason} {rule.exception_note} Routed for review "
                    "rather than decided: guessing eligible claims credit the "
                    "company may not be entitled to, and guessing ineligible "
                    "leaks money, which is the problem this register exists to "
                    "fix."
                ),
                reference=rule.reference,
                tax_split=None,
                claimable_amount=Decimal("0.00"),
                amount_at_stake=tax,
                needs_review=True,
            )
        return EligibilityVerdict(
            eligibility=Eligibility.INELIGIBLE,
            reason=rule.reason,
            reference=rule.reference,
            tax_split=None,
            claimable_amount=Decimal("0.00"),
            amount_at_stake=tax,
            needs_review=False,
        )

    try:
        split = determine_tax_split(
            supplier_state_code=invoice.supplier_state_code,
            place_of_supply_state_code=invoice.place_of_supply_state_code,
            recipient_state_code=invoice.recipient_state_code,
        )
    except EligibilityError as exc:
        return EligibilityVerdict(
            eligibility=Eligibility.UNCERTAIN,
            reason=(
                f"{exc} Until the states are known, whether this is IGST or "
                "another state's CGST and SGST cannot be established, and that "
                "decides whether it is claimable at all."
            ),
            reference="Place of supply",
            tax_split=None,
            claimable_amount=Decimal("0.00"),
            amount_at_stake=tax,
            needs_review=True,
        )

    if split is TaxSplit.CGST_SGST_OTHER_STATE:
        return EligibilityVerdict(
            eligibility=Eligibility.INELIGIBLE,
            reason=(
                f"The supply was intra-state in state {invoice.place_of_supply_state_code} "
                f"and this entity is registered in {invoice.recipient_state_code}. "
                "The CGST and SGST charged belong to a state the entity has no "
                "registration in, so the credit is not available - this is the "
                "hotel-bill case, and it is the most commonly claimed credit "
                "that should not have been."
            ),
            reference="Place of supply",
            tax_split=split,
            claimable_amount=Decimal("0.00"),
            amount_at_stake=tax,
            needs_review=False,
        )

    if split is TaxSplit.IGST and invoice.igst <= 0 and (invoice.cgst > 0 or invoice.sgst > 0):
        return EligibilityVerdict(
            eligibility=Eligibility.UNCERTAIN,
            reason=(
                "The supply is inter-state and should carry IGST, but the "
                "invoice shows CGST and SGST. Either the place of supply was "
                "extracted wrongly or the vendor invoiced incorrectly. Both are "
                "worth a person looking, and neither is safe to decide here."
            ),
            reference="Place of supply",
            tax_split=split,
            claimable_amount=Decimal("0.00"),
            amount_at_stake=tax,
            needs_review=True,
        )

    if split is TaxSplit.CGST_SGST and invoice.igst > 0:
        return EligibilityVerdict(
            eligibility=Eligibility.UNCERTAIN,
            reason=(
                "The supply is intra-state and should carry CGST and SGST, but "
                "the invoice shows IGST. The credit may still be available and "
                "the mismatch has to be resolved with the vendor first."
            ),
            reference="Place of supply",
            tax_split=split,
            claimable_amount=Decimal("0.00"),
            amount_at_stake=tax,
            needs_review=True,
        )

    if tax <= 0:
        return EligibilityVerdict(
            eligibility=Eligibility.INELIGIBLE,
            reason=(
                "No tax was charged on this invoice, so there is no input "
                "credit. An exempt or composition supply looks exactly like "
                "this and is not an error."
            ),
            reference="No tax charged",
            tax_split=split,
            claimable_amount=Decimal("0.00"),
            amount_at_stake=Decimal("0.00"),
            needs_review=False,
        )

    # Cess is excluded from the claimable figure: compensation cess credit can
    # generally only be set off against cess, so folding it into an ITC total
    # overstates what is usable against GST.
    claimable = money(invoice.cgst + invoice.sgst + invoice.igst)
    return EligibilityVerdict(
        eligibility=Eligibility.ELIGIBLE,
        reason=(
            f"Business expense with a valid GSTIN, {split.value.replace('_', '/')} "
            "consistent with the place of supply, and not in the blocked list. "
            + (
                f"Cess of {money(invoice.cess)} is excluded - cess credit is "
                "generally usable only against cess."
                if invoice.cess > 0
                else "No cess on this invoice."
            )
        ),
        reference="Section 16, CGST Act",
        tax_split=split,
        claimable_amount=claimable,
        amount_at_stake=claimable,
        needs_review=False,
    )
