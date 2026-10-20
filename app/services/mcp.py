"""AskMira as an MCP server (AI-04): read-only tools for Claude and other clients.

═══════════════════════════════════════════════════════════════════════════════
WHAT IT IS
═══════════════════════════════════════════════════════════════════════════════

A Model Context Protocol server over Streamable HTTP, stateless: one JSON-RPC
request per POST, one JSON response back, no server-held session. A workspace
admin creates an API key with the scopes they want an assistant to have, and
the client sends it as a Bearer token. Claude Code, for example:

    claude mcp add --transport http askmira https://<host>/api/v1/mcp \\
        --header "Authorization: Bearer amk_..."

Or a person adds AskMira as a connector (claude.ai, ChatGPT) by its URL alone:
the client discovers the OAuth server, the person signs in and approves, and
the tools then act as that person, within the read scopes their role allows
(`app.services.oauth`).

═══════════════════════════════════════════════════════════════════════════════
WHAT IT WILL NOT DO
═══════════════════════════════════════════════════════════════════════════════

Write. Every tool reads, and says so in its annotations. A tool appears only if
the key holds its scope, and is refused if called without it. Nothing returned
carries pay, bank, statutory identifiers, health or the reason someone gave for
their leave — the same boundary as the customer API, because an assistant that
reads this is just another integration. Each call is written to the audit log
as an MCP action, with the tool and its arguments.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.attendance import AttendanceDay
from app.models.audit import AuditAction
from app.models.employee import Employee, EmploymentStatus
from app.models.leave import LeaveRequest, LeaveRequestStatus, LeaveType
from app.models.org import Department, Location
from app.models.user import User
from app.services import audit

__all__ = ["PROTOCOL_VERSIONS", "TOOLS", "ToolError", "handle", "run_tool"]

#: Newest first. A client asking for one of these gets it; anything else gets
#: the newest, which is what the protocol's negotiation asks for.
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_RANGE_DAYS = 93

INSTRUCTIONS = (
    "AskMira is an HR system for a company in India. These tools read its people "
    "directory, departments, locations, approved leave and attendance. They cannot "
    "change anything, and they never return pay, bank, tax, health or leave reasons."
)


class ToolError(Exception):
    """A tool call that cannot be answered; the message goes back to the client."""


def _read_only(title: str) -> dict[str, Any]:
    return {"title": title, "readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}


def _date_range_schema(description: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "from": {"type": "string", "format": "date", "description": "First day, YYYY-MM-DD."},
            "to": {"type": "string", "format": "date", "description": "Last day, YYYY-MM-DD."},
        },
        "required": ["from", "to"],
        "additionalProperties": False,
        "description": description,
    }


TOOLS: dict[str, dict[str, Any]] = {
    "search_employees": {
        "scope": "employees:read",
        "description": (
            "Find people in the directory by name, employee code, title or email. "
            "Returns code, name, title, department, manager, status and work email."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Text to match; empty lists everyone."},
                "include_exited": {"type": "boolean", "description": "Include people who left."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "additionalProperties": False,
        },
        "annotations": _read_only("Search the people directory"),
    },
    "list_departments": {
        "scope": "org:read",
        "description": "Every department, with its code and parent department.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": _read_only("List departments"),
    },
    "list_locations": {
        "scope": "org:read",
        "description": "Every office location, with its city and state code.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": _read_only("List locations"),
    },
    "who_is_out": {
        "scope": "leave:read",
        "description": (
            "Approved leave overlapping a date range: who, which leave type, which days, "
            "and whether it is a half day. Never the reason given."
        ),
        "inputSchema": _date_range_schema("At most 93 days."),
        "annotations": _read_only("Who is on leave"),
    },
    "attendance_summary": {
        "scope": "attendance:read",
        "description": (
            "Per person over a date range: days present, absent, on leave, half days and "
            "late arrivals, from the attendance AskMira has judged."
        ),
        "inputSchema": _date_range_schema("At most 93 days."),
        "annotations": _read_only("Attendance summary"),
    },
}


def _name(person: Employee) -> str:
    return person.display_name or " ".join(p for p in (person.first_name, person.last_name) if p)


def _dates(arguments: dict[str, Any]) -> tuple[date, date]:
    try:
        start = date.fromisoformat(str(arguments.get("from", "")))
        end = date.fromisoformat(str(arguments.get("to", "")))
    except ValueError as exc:
        raise ToolError("Give from and to as dates, YYYY-MM-DD.") from exc
    if end < start or (end - start).days > MAX_RANGE_DAYS:
        raise ToolError(f"Ask for at most {MAX_RANGE_DAYS} days, with from before to.")
    return start, end


async def _search_employees(
    session: AsyncSession, actor: User, arguments: dict[str, Any]
) -> dict[str, Any]:
    query = str(arguments.get("query") or "").strip()
    limit = max(1, min(int(arguments.get("limit") or 25), 100))
    stmt = select(Employee).where(Employee.deleted_at.is_(None))
    if not arguments.get("include_exited"):
        stmt = stmt.where(Employee.status != EmploymentStatus.EXITED)
    if query:
        like = f"%{query}%"
        stmt = stmt.where(
            or_(
                Employee.first_name.ilike(like),
                # A full name ("Meera Nair") spans both columns.
                func.concat_ws(" ", Employee.first_name, Employee.last_name).ilike(like),
                Employee.last_name.ilike(like),
                Employee.display_name.ilike(like),
                Employee.employee_code.ilike(like),
                Employee.designation.ilike(like),
                Employee.work_email.ilike(like),
            )
        )
    people = (
        await session.scalars(
            tenant_scoped(stmt.order_by(Employee.first_name).limit(limit), Employee, actor)
        )
    ).all()
    departments = {
        d.id: d.name
        for d in (await session.scalars(tenant_scoped(select(Department), Department, actor))).all()
    }
    manager_ids = {p.manager_id for p in people if p.manager_id}
    managers = (
        {
            m.id: _name(m)
            for m in (
                await session.scalars(
                    tenant_scoped(
                        select(Employee).where(Employee.id.in_(manager_ids)), Employee, actor
                    )
                )
            ).all()
        }
        if manager_ids
        else {}
    )
    return {
        "count": len(people),
        "people": [
            {
                "employee_code": p.employee_code,
                "name": _name(p),
                "title": p.designation,
                "department": departments.get(p.department_id),
                "manager": managers.get(p.manager_id),
                "status": p.status.value,
                "work_email": p.work_email,
            }
            for p in people
        ],
    }


async def _list_departments(
    session: AsyncSession, actor: User, arguments: dict[str, Any]
) -> dict[str, Any]:
    rows = (
        await session.scalars(
            tenant_scoped(select(Department).order_by(Department.name), Department, actor)
        )
    ).all()
    names = {row.id: row.name for row in rows}
    return {
        "departments": [
            {"name": row.name, "code": row.code, "parent": names.get(row.parent_id)} for row in rows
        ]
    }


async def _list_locations(
    session: AsyncSession, actor: User, arguments: dict[str, Any]
) -> dict[str, Any]:
    rows = (
        await session.scalars(
            tenant_scoped(select(Location).order_by(Location.name), Location, actor)
        )
    ).all()
    return {
        "locations": [
            {
                "name": row.name,
                "city": row.city,
                "state_code": row.state_code,
            }
            for row in rows
        ]
    }


async def _who_is_out(
    session: AsyncSession, actor: User, arguments: dict[str, Any]
) -> dict[str, Any]:
    start, end = _dates(arguments)
    rows = (
        await session.execute(
            tenant_scoped(
                select(LeaveRequest, Employee, LeaveType)
                .join(Employee, Employee.id == LeaveRequest.employee_id)
                .join(LeaveType, LeaveType.id == LeaveRequest.leave_type_id)
                .where(
                    LeaveRequest.status == LeaveRequestStatus.APPROVED,
                    LeaveRequest.start_date <= end,
                    LeaveRequest.end_date >= start,
                )
                .order_by(LeaveRequest.start_date),
                LeaveRequest,
                actor,
            )
        )
    ).all()
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "leave": [
            {
                "employee_code": person.employee_code,
                "name": _name(person),
                "leave_type": kind.name,
                "start_date": request.start_date.isoformat(),
                "end_date": request.end_date.isoformat(),
                "half_day": request.is_half_day,
            }
            for request, person, kind in rows
        ],
    }


async def _attendance_summary(
    session: AsyncSession, actor: User, arguments: dict[str, Any]
) -> dict[str, Any]:
    start, end = _dates(arguments)
    rows = (
        await session.execute(
            tenant_scoped(
                select(AttendanceDay, Employee)
                .join(Employee, Employee.id == AttendanceDay.employee_id)
                .where(
                    AttendanceDay.attendance_date >= start,
                    AttendanceDay.attendance_date <= end,
                ),
                AttendanceDay,
                actor,
            )
        )
    ).all()
    by_person: dict[uuid.UUID, dict[str, Any]] = {}
    for day, person in rows:
        entry = by_person.setdefault(
            person.id,
            {
                "employee_code": person.employee_code,
                "name": _name(person),
                "present": 0,
                "absent": 0,
                "on_leave": 0,
                "half_day": 0,
                "late": 0,
            },
        )
        kind = day.status.value
        if kind in ("present", "absent", "on_leave", "half_day"):
            entry[kind] += 1
        if (day.late_by_minutes or 0) > 0:
            entry["late"] += 1
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "days_in_range": (end - start + timedelta(days=1)).days,
        "people": sorted(by_person.values(), key=lambda e: e["employee_code"]),
    }


_HANDLERS = {
    "search_employees": _search_employees,
    "list_departments": _list_departments,
    "list_locations": _list_locations,
    "who_is_out": _who_is_out,
    "attendance_summary": _attendance_summary,
}


async def run_tool(
    session: AsyncSession, actor: User, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    """One read tool, run as `actor`. Ask Mira calls the same tools through here.

    The FRD's single gateway (5.10, 7.1): the in-app companion and outside agents
    read through the same handlers, so neither has a wider view than the other.
    Which tools a caller may use is decided by the caller's scopes, not here.
    """
    handler = _HANDLERS.get(name)
    if handler is None:
        raise ToolError(f"No tool named {name!r}.")
    return await handler(session, actor, arguments)


def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


async def handle(
    session: AsyncSession, actor: User, scopes: frozenset[str], message: Any
) -> dict[str, Any] | None:
    """One JSON-RPC message in, one response out (None for a notification)."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _error(None, -32600, "Send one JSON-RPC 2.0 request object.")
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}
    if not isinstance(method, str):
        return _error(request_id, -32600, "A request needs a method.")
    if "id" not in message:
        # Notifications — `notifications/initialized` and the like — need no reply.
        return None

    if method == "initialize":
        asked = params.get("protocolVersion")
        version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return _result(
            request_id,
            {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "askmira", "title": "AskMira HR", "version": "1.0.0"},
                "instructions": INSTRUCTIONS,
            },
        )
    if method == "ping":
        return _result(request_id, {})
    if method == "tools/list":
        return _result(
            request_id,
            {
                "tools": [
                    {
                        "name": name,
                        "title": spec["annotations"]["title"],
                        "description": spec["description"],
                        "inputSchema": spec["inputSchema"],
                        "annotations": spec["annotations"],
                    }
                    for name, spec in TOOLS.items()
                    if spec["scope"] in scopes
                ]
            },
        )
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        spec = TOOLS.get(name) if isinstance(name, str) else None
        if spec is None:
            return _error(request_id, -32602, f"No tool named {name!r}.")
        if spec["scope"] not in scopes:
            return _error(request_id, -32602, f"This key does not have {spec['scope']}.")
        if not isinstance(arguments, dict):
            return _error(request_id, -32602, "Tool arguments must be an object.")
        try:
            output = await _HANDLERS[name](session, actor, arguments)
        except (ToolError, ValueError, TypeError) as exc:
            return _result(
                request_id,
                {"content": [{"type": "text", "text": str(exc)}], "isError": True},
            )
        await audit.record(
            session,
            tenant_id=actor.tenant_id,
            action=AuditAction.AI_ACTION,
            entity_type="mcp_tool",
            # A person connected through OAuth is named; an API key has no person.
            actor_user_id=getattr(actor, "id", None),
            actor_type="mcp_agent",
            summary=f"MCP tool {name} called by {actor.full_name}",
            ai_context={"tool": name, "arguments": arguments},
        )
        await session.commit()
        return _result(
            request_id,
            {
                "content": [{"type": "text", "text": json.dumps(output, ensure_ascii=False)}],
                "structuredContent": output,
                "isError": False,
            },
        )
    return _error(request_id, -32601, f"Method {method!r} is not supported.")
