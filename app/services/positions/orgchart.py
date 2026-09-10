"""The position-based org chart, holes included.

Two things this exists to do that an employee-to-employee chart cannot.

**It survives a manager's exit.** In an employee chart, when a manager leaves,
everybody who reported to them is orphaned until somebody re-parents them one by
one. In a position chart the reporting line is between *seats*, so the structure
is unchanged and the seat is simply empty. That is the practical argument for
positions and it is worth showing in a demo.

**It shows the vacant seats.** A chart of only filled roles is a chart of who is
here; a chart including the holes is a chart of what the organisation is meant to
be, which is the one you plan against. A team of eight showing as six looks
understaffed; showing as six of eight tells you it is two short and which two.

Cycles are detected rather than trusted. Somebody will eventually set two
positions as reporting to each other, and a chart builder that recurses on that
hangs the request rather than saying so.

Pure. No database.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.services.positions.headcount import (
    OccupancySnapshot,
    PositionStatus,
    money,
)

__all__ = ["ChartNode", "OrgChartError", "build_chart", "find_cycles"]

#: Deeper than this and the structure is almost certainly a cycle rather than a
#: hierarchy. Indian mid-market org charts run to six or seven levels.
MAX_DEPTH = 20


class OrgChartError(ValueError):
    """A structure this module will not render."""


@dataclass(slots=True)
class ChartNode:
    """One seat on the chart, filled or not."""

    position_id: str
    code: str
    title: str
    status: PositionStatus
    fte: Decimal
    department_name: str
    grade: str | None
    #: `None` where the seat is empty. The whole point.
    holder_name: str | None
    #: Somebody covering an empty seat. Rendered differently from a holder,
    #: because "Priya (acting)" over an empty box is the true picture and
    #: "Priya" is not.
    covering_names: tuple[str, ...]
    children: list[ChartNode] = field(default_factory=list)

    @property
    def is_vacant(self) -> bool:
        return self.holder_name is None

    def descendants(self) -> int:
        return sum(1 + child.descendants() for child in self.children)

    def vacant_below(self) -> int:
        """Empty seats anywhere beneath this one.

        A manager reading their own subtree wants one number for "how short am
        I", not a count they have to assemble by eye.
        """
        return sum(
            (1 if child.is_vacant else 0) + child.vacant_below()
            for child in self.children
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "position_id": self.position_id,
            "code": self.code,
            "title": self.title,
            "status": self.status.value,
            "fte": money(self.fte),
            "department": self.department_name,
            "grade": self.grade,
            "holder": self.holder_name,
            "vacant": self.is_vacant,
            "covering": list(self.covering_names),
            "reports": self.descendants(),
            "vacant_below": self.vacant_below(),
            "children": [child.as_dict() for child in self.children],
        }


def find_cycles(snapshots: list[OccupancySnapshot]) -> list[list[str]]:
    """Reporting loops, named by position code.

    Somebody eventually sets two positions as reporting to each other, usually
    during a reorg with two people editing at once. Returned rather than raised
    so the caller can render the rest of the chart and say which part is broken —
    refusing to draw anything because one branch is wrong is not helpful to the
    person who has to fix it.
    """
    parent = {
        s.position.position_id: s.position.reports_to_position_id for s in snapshots
    }
    codes = {s.position.position_id: s.position.code for s in snapshots}

    cycles: list[list[str]] = []
    seen_globally: set[str] = set()

    for start in parent:
        if start in seen_globally:
            continue
        path: list[str] = []
        on_path: set[str] = set()
        node: str | None = start
        while node is not None and node in parent:
            if node in on_path:
                loop = path[path.index(node):]
                cycles.append([codes.get(p, p) for p in loop])
                break
            if node in seen_globally:
                break
            path.append(node)
            on_path.add(node)
            node = parent[node]
        seen_globally.update(path)

    return cycles


def build_chart(
    snapshots: list[OccupancySnapshot], *, root_position_id: str | None = None
) -> tuple[list[ChartNode], list[str]]:
    """The chart, and anything wrong with it.

    Returns `(roots, problems)`. A cycle produces a problem and the positions
    inside it are left out of the tree rather than recursed into — the alternative
    is a request that never returns.
    """
    problems: list[str] = []

    live = [
        s for s in snapshots if s.position.status is not PositionStatus.ABOLISHED
    ]
    by_id = {s.position.position_id: s for s in live}

    cycles = find_cycles(live)
    in_a_cycle: set[str] = set()
    for cycle in cycles:
        codes = " -> ".join(cycle)
        problems.append(
            f"These positions report to each other in a loop: {codes}. They are left "
            "off the chart until one of the reporting lines is corrected."
        )
        for snapshot in live:
            if snapshot.position.code in cycle:
                in_a_cycle.add(snapshot.position.position_id)

    nodes: dict[str, ChartNode] = {}
    for snapshot in live:
        if snapshot.position.position_id in in_a_cycle:
            continue
        position = snapshot.position
        holder = snapshot.primary_holder
        nodes[position.position_id] = ChartNode(
            position_id=position.position_id,
            code=position.code,
            title=position.title,
            status=position.status,
            fte=position.fte,
            department_name=position.department_name or "(no department)",
            grade=position.grade,
            holder_name=(
                (holder.employee_label or holder.employee_id) if holder else None
            ),
            covering_names=tuple(
                h.employee_label or h.employee_id
                for h in snapshot.holders
                if not h.counts
            ),
        )

    children: dict[str | None, list[str]] = defaultdict(list)
    for position_id, node in nodes.items():
        parent_id = by_id[position_id].position.reports_to_position_id
        # A position reporting to one that has been abolished, or to one inside a
        # cycle, becomes a root rather than disappearing. Losing a whole subtree
        # because its parent was deleted is the failure this guards against.
        if parent_id is not None and parent_id not in nodes:
            problems.append(
                f"{node.code} reports to a position that is not on the chart. It is "
                "shown at the top level so its own team is not lost with it."
            )
            parent_id = None
        children[parent_id].append(position_id)

    def attach(position_id: str, depth: int) -> ChartNode:
        if depth > MAX_DEPTH:
            raise OrgChartError(
                f"The reporting structure is more than {MAX_DEPTH} levels deep, which "
                "is almost certainly a loop the cycle check did not catch rather than "
                "a real hierarchy."
            )
        node = nodes[position_id]
        node.children = [
            attach(child_id, depth + 1)
            for child_id in sorted(children.get(position_id, ()), key=lambda i: nodes[i].code)
        ]
        return node

    if root_position_id is not None:
        if root_position_id not in nodes:
            raise OrgChartError(
                "That position is not on the chart — it may have been abolished, or it "
                "may be inside a reporting loop."
            )
        return [attach(root_position_id, 0)], problems

    roots = [
        attach(position_id, 0)
        for position_id in sorted(children.get(None, ()), key=lambda i: nodes[i].code)
    ]
    return roots, problems


def flatten(nodes: list[ChartNode]) -> list[dict[str, Any]]:
    """The chart as rows, for a table view or an export.

    Depth is carried so a flat list can still be indented, rather than losing the
    structure the moment somebody wants it in a spreadsheet.
    """
    out: list[dict[str, Any]] = []

    def walk(node: ChartNode, depth: int, path: str) -> None:
        out.append(
            {
                "depth": depth,
                "path": path,
                "position_id": node.position_id,
                "code": node.code,
                "title": node.title,
                "holder": node.holder_name,
                "vacant": node.is_vacant,
                "covering": list(node.covering_names),
                "status": node.status.value,
                "fte": money(node.fte),
            }
        )
        for child in node.children:
            walk(child, depth + 1, f"{path} / {child.code}" if path else child.code)

    for node in nodes:
        walk(node, 0, node.code)
    return out
