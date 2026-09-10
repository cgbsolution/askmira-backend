"""Expense policy, mileage, per-diem, travel advances and multi-currency.

ExpenseGenie captures receipts and governs nothing. This package is the
governance: policy evaluated at capture rather than at approval, rates that are
effective-dated, advances that settle against the trip they funded, and foreign
claims that keep both the incurred amount and the converted one.

Everything except `service.py` is pure.
"""
