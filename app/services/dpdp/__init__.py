"""DPDP Act compliance, built as mechanism rather than policy documents.

Everything in this package except `service.py` is pure: no session, no models,
no FastAPI. The classification catalogue, the lawful-basis mapping, the
statutory clocks and the erasure reconciliation are all decidable from their
arguments, which is what makes them testable against the cases that matter and
reviewable by somebody who is not a Python programmer.

**This needs legal review, not only engineering review.** The mechanism is
built here; the interpretation — which purpose rests on which lawful basis,
which retention obligation binds which record — is counsel's call, and every
place that judgement is encoded says so.
"""
