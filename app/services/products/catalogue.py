"""The products a workspace can be linked to, and who may use each.

A product *replaces* one AskMira module for a linked workspace: the portal hides
that module's pages and shows a launcher instead. The role a person gets in the
product is decided here, from their AskMira role — the product never decides it.
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
        return None


PRODUCTS: dict[str, Product] = {
    "interview": Product(key="interview", name="Interviews", replaces="hiring"),
    "expense": Product(key="expense", name="Expenses", replaces="expenses"),
}


def product_or_none(key: str) -> Product | None:
    return PRODUCTS.get(key)
