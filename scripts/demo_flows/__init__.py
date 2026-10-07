"""The demo company's flows, driven through the real API.

Every step here is a request a person would make in the portal, made as that
person: an employee applies for leave, their manager approves it, HR locks
attendance, one admin computes payroll and another approves it. Nothing is
written straight to the database except the logins `seed_demo` creates, which
the product itself has no way to create.

That is the point of seeding this way: if a flow is broken, the seed fails at
the step that broke, naming the request, the person and the server's reason.
"""
