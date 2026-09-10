"""Retention and deletion: the conflict resolved explicitly.

DPDP requires purpose-limited retention. Indian statute requires payroll and
statutory records be kept for defined periods. Those obligations point in
opposite directions and the resolution cannot be "keep everything forever",
which is what every system does by default.

`taxonomy.py` states the resolution as three tiers. `anonymise.py` implements
the middle one, honestly. `policy.py` holds the floors a tenant cannot go below.
`planner.py` decides what happens tonight and what needs a person to approve
first.

Everything here except `service.py` is pure.
"""
