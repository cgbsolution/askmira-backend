"""The products a workspace can be linked to, and who may use each.

A product *replaces* AskMira's own module for a linked workspace: the portal
hides that module's pages and shows the product's records instead (the
helpdesk replaces two: Helpdesk and Assets). The role a person gets in the
product is decided here, from their AskMira role — the product never decides it
(the helpdesk keeps the engineers and admins its own IT team sets up, and
treats this role as the least a person gets).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models.user import UserRole

_HR = {UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.SUB_ADMIN}


@dataclass(frozen=True, slots=True)
class Product:
    key: str
    name: str
    #: The AskMira nav section the product stands in for.
    replaces: str
    #: The product's own name, shown wherever its data appears in AskMira.
    brand: str

    def role_for(self, role: UserRole, *, is_manager: bool) -> str | None:
        """The person's role in the product, or None when they may not use it."""
        if self.key == "interview":
            # Hiring is HR's and hiring managers' work, as the built-in module was.
            if role in _HR:
                return "admin"
            return "recruiter" if is_manager else None
        if self.key == "expense":
            # Everybody files expenses; HR runs the workspace.
            return "admin" if role in _HR else "employee"
        if self.key == "helpdesk":
            # Everybody raises tickets and has assets; HR looks after the company there.
            return "admin" if role in _HR else "user"
        return None


PRODUCTS: dict[str, Product] = {
    "interview": Product(
        key="interview", name="Interviews", replaces="hiring", brand="AnytimeHire"
    ),
    "expense": Product(key="expense", name="Expenses", replaces="expenses", brand="ExpenseGenie"),
    "helpdesk": Product(key="helpdesk", name="Helpdesk", replaces="helpdesk", brand="CGB Helpdesk"),
}


def product_or_none(key: str) -> Product | None:
    return PRODUCTS.get(key)
