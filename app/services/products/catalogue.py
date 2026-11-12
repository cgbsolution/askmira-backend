"""The products a workspace can be linked to, and who may use each.

A product *replaces* AskMiraa's own module for a linked workspace: the portal
hides that module's pages and shows the product's records instead (the
helpdesk replaces two: Helpdesk and Assets). The role a person gets in the
product is decided here, from their AskMiraa role — the product never decides it.
The helpdesk is the exception: its roles are its IT team's to give, and reach
every organization it serves, so AskMiraa's "admin" there only lets HR read
their own company's tickets and assets.

Saleinx (the sales CRM) and ProcureFlow (procure-to-pay) go further: they add
to AskMiraa rather than replace a module, and their roles are wholly their own —
a rep, a manager or a viewer in Saleinx; a requester, an approver or finance in
ProcureFlow. AskMiraa never grants one. Their people sync only links the people
their own admins invited, by email, and answers with each one's role there
(`ProductLink.member_roles`); that answer, not AskMiraa's role, decides what
Ask Miraa offers them, and the product checks again on every call.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models.user import UserRole

_HR = {UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.SUB_ADMIN}


@dataclass(frozen=True, slots=True)
class Product:
    key: str
    name: str
    #: The AskMiraa nav section the product stands in for ("" when it adds one).
    replaces: str
    #: The product's own name, shown wherever its data appears in AskMiraa.
    brand: str
    #: The person's role there comes from the product (its people sync), not from AskMiraa.
    roles_from_product: bool = False

    def role_for(self, role: UserRole, *, is_manager: bool) -> str | None:
        """The person's role in the product, or None when they may not use it.

        For a product whose roles are its own this is only who AskMiraa sends in
        its people sync ("member": everyone, for the product to match by email);
        `member_role` says what they are there.
        """
        if self.key == "interview":
            # Hiring is HR's and hiring managers' work, as the built-in module was.
            if role in _HR:
                return "admin"
            return "recruiter" if is_manager else None
        if self.key == "expense":
            # Everybody files expenses; HR runs the workspace.
            return "admin" if role in _HR else "employee"
        if self.key == "helpdesk":
            # Everybody raises tickets and holds assets; HR read their whole company's.
            return "admin" if role in _HR else "user"
        if self.roles_from_product:
            return "member"
        return None


PRODUCTS: dict[str, Product] = {
    "interview": Product(
        key="interview", name="Interviews", replaces="hiring", brand="AnytimeHire"
    ),
    "expense": Product(key="expense", name="Expenses", replaces="expenses", brand="ExpenseGenie"),
    "helpdesk": Product(key="helpdesk", name="Helpdesk", replaces="helpdesk", brand="CGB Helpdesk"),
    "saleinx": Product(
        key="saleinx", name="Sales", replaces="", brand="Saleinx", roles_from_product=True
    ),
    "procurement": Product(
        key="procurement",
        name="Procurement",
        replaces="",
        brand="ProcureFlow",
        roles_from_product=True,
    ),
    # Accounting, GST and finance (E:\ask-mira\finance). Roles are its own (Admin,
    # Accountant, Staff, Viewer or custom), like Saleinx and ProcureFlow. The brand
    # is a working name until the product is named.
    "finance": Product(
        key="finance", name="Finance", replaces="", brand="Finance", roles_from_product=True
    ),
}


def product_or_none(key: str) -> Product | None:
    return PRODUCTS.get(key)


def member_role(product: Product, member_roles: dict | None, email: str) -> str | None:
    """A person's role in a product whose roles are its own, from its last people sync."""
    if not product.roles_from_product or not member_roles:
        return None
    role = member_roles.get(email.lower())
    return str(role) if role else None
