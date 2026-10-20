"""No API route repeats its router's prefix (`/culture/culture/...`).

A route declared with the full path inside a router that already has that
prefix is mounted twice over, and the portal's call to the intended path gets
a bare 404. This is how the culture milestones endpoints went missing.
"""

from __future__ import annotations

from app.main import app


def test_no_route_repeats_its_prefix() -> None:
    doubled = []
    for route in app.routes:
        path = getattr(route, "path", "")
        parts = [part for part in path.split("/") if part]
        if len(parts) >= 4 and parts[:2] == ["api", "v1"] and parts[2] == parts[3]:
            doubled.append(path)
    assert doubled == []


def test_culture_milestone_rules_are_where_the_portal_calls_them() -> None:
    paths = {getattr(route, "path", "") for route in app.routes}
    assert "/api/v1/culture/milestones" in paths
