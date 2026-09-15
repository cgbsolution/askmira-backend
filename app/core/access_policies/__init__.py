"""Per-module declarations of signed-in endpoints that carry no role gate.

`app.core.access_policy` merges these into `UNGATED_ENDPOINTS`. One file per
module, so work on two modules at once never edits the same declaration, and a
key declared in two modules fails at import rather than one silently winning.

See `app.core.access_policy` for the rules and what the invariants check.
"""
