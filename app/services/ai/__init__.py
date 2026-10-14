"""The AI layer: one client, one governance gate, and the features built on them.

Every model call in AskMira goes through `client.structured` and is gated by
`governance.require` — the tenant's switch, the person's consent, and a
cross-border transfer row written in the same transaction as the call's log.
"""
