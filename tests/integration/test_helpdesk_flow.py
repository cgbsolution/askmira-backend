"""HR helpdesk, through HTTP: raise, assign, reply, resolve, rate — and who sees what.

These go through the real app so the multipart parsing, the agent/requester
split and the 404-not-403 rule are exercised the way the portal exercises them.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import (
    AuditLog,
    HelpdeskTicket,
    HelpdeskTicketStatus,
    Notification,
    Tenant,
    User,
    UserRole,
)
from app.services.helpdesk import service as helpdesk
from app.services.scheduler.jobs import system_actor
from tests.integration import platform_support
from tests.integration.conftest import requires_db
from tests.integration.platform_support import bearer, make_person

pytestmark = requires_db

client = platform_support.client

PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


@pytest.fixture(autouse=True)
def _storage_root(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "FILE_STORAGE_ROOT", str(tmp_path / "store"))
    return tmp_path / "store"


@pytest.fixture
async def people(session: AsyncSession, tenant: Tenant) -> dict[str, User]:
    return {
        "anita": await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Anita Rao"),
        "bala": await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Bala Iyer"),
        "ops": await make_person(session, tenant, role=UserRole.SUB_ADMIN, name="Hari Ops"),
        "admin": await make_person(session, tenant, role=UserRole.ADMIN, name="Asha Admin"),
        "officer": await make_person(session, tenant, role=UserRole.EMPLOYEE, name="Gita Officer"),
    }


async def _category(client, admin: User, name: str) -> dict:
    listed = await client.get("/api/v1/helpdesk/categories", headers=bearer(admin))
    assert listed.status_code == 200, listed.text
    return next(c for c in listed.json() if c["name"] == name)


async def _raise(
    client, user: User, category_id: str, *, subject="Salary short", file=None, employee_id=None
):
    data = {
        "category_id": category_id,
        "subject": subject,
        "description": "My September salary is 4,000 short.",
        "priority": "high",
    }
    if employee_id:
        data["employee_id"] = employee_id
    files = {"file": ("slip.pdf", file, "application/pdf")} if file else None
    return await client.post(
        "/api/v1/helpdesk/tickets", headers=bearer(user), data=data, files=files
    )


async def _events(session: AsyncSession, key: str) -> set[uuid.UUID]:
    """Who was sent this event, on any channel."""
    rows = await session.scalars(
        select(Notification.recipient_user_id).where(Notification.event_key == key)
    )
    return set(rows.all())


# ═══════════════════════════════════════════════════════════════════════════
# The main flow
# ═══════════════════════════════════════════════════════════════════════════


async def test_raise_assign_reply_resolve_rate(client, session, people) -> None:
    anita, ops, admin = people["anita"], people["ops"], people["admin"]

    # Categories exist only once an admin asks for them, and asking twice adds nothing.
    assert (await client.get("/api/v1/helpdesk/categories", headers=bearer(anita))).json() == []
    refused = await client.post("/api/v1/helpdesk/categories/standard", headers=bearer(ops))
    assert refused.status_code == 403
    preview = await client.get("/api/v1/helpdesk/categories/standard", headers=bearer(admin))
    assert len(preview.json()) == 8
    assert (await client.get("/api/v1/helpdesk/categories", headers=bearer(admin))).json() == []
    added = await client.post("/api/v1/helpdesk/categories/standard", headers=bearer(admin))
    assert added.status_code == 200, added.text
    assert len(added.json()["created"]) == 8 and added.json()["skipped"] == []
    again = (
        await client.post("/api/v1/helpdesk/categories/standard", headers=bearer(admin))
    ).json()
    assert again["created"] == [] and len(again["skipped"]) == 8
    payroll = await _category(client, admin, "Payroll & salary query")
    assert payroll["ticket_count"] == 0
    assert (await _category(client, anita, "Payroll & salary query"))["ticket_count"] is None

    # Raise, with a receipt attached. Numbers are per tenant and sequential.
    first = await _raise(client, anita, payroll["id"], file=PDF)
    assert first.status_code == 201, first.text
    ticket = first.json()
    assert ticket["ticket_number"] == "HD-00001"
    assert ticket["status"] == "open" and ticket["assignee_user_id"] is None
    assert ticket["sla_state"] == "on_track" and ticket["raised_on_behalf"] is False
    created = datetime.fromisoformat(ticket["created_at"])
    assert datetime.fromisoformat(ticket["due_at"]) - created == timedelta(hours=48)
    second = await _raise(client, anita, payroll["id"], subject="Form 16")
    assert second.json()["ticket_number"] == "HD-00002"
    # No owner, so HR hears about it — everybody in HR, and not the requester.
    assert await _events(session, "helpdesk.ticket.created") >= {ops.id, admin.id}
    assert anita.id not in await _events(session, "helpdesk.ticket.created")

    tid = ticket["id"]
    mine = await client.get("/api/v1/helpdesk/tickets/mine?status=active", headers=bearer(anita))
    assert [t["ticket_number"] for t in mine.json()] == ["HD-00002", "HD-00001"]

    # The queue, and who it can go to.
    queue = await client.get(
        "/api/v1/helpdesk/queue?assignee=unassigned&search=HD-00001", headers=bearer(ops)
    )
    assert [t["id"] for t in queue.json()] == [tid]
    assignees = await client.get(f"/api/v1/helpdesk/tickets/{tid}/assignees", headers=bearer(ops))
    assert {a["user_id"] for a in assignees.json()} >= {str(ops.id), str(admin.id)}
    assert str(anita.id) not in {a["user_id"] for a in assignees.json()}
    assigned = await client.post(
        f"/api/v1/helpdesk/tickets/{tid}/assign",
        headers=bearer(admin),
        json={"assignee_user_id": str(ops.id)},
    )
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["assignee_name"] == "Hari Ops"
    assert await _events(session, "helpdesk.ticket.assigned") == {ops.id}

    # A public reply is the first response and starts the work.
    replied = await client.post(
        f"/api/v1/helpdesk/tickets/{tid}/comments",
        headers=bearer(ops),
        data={"body": "Looking into it. Which bank account?"},
    )
    assert replied.status_code == 200, replied.text
    assert replied.json()["status"] == "in_progress"
    assert replied.json()["first_response_at"] is not None
    assert anita.id in await _events(session, "helpdesk.ticket.replied")

    # An internal note, with a file, is for agents only.
    note = await client.post(
        f"/api/v1/helpdesk/tickets/{tid}/comments",
        headers=bearer(ops),
        data={"body": "Payroll run shows an LOP day.", "is_internal": "true"},
        files={"file": ("lop.png", PNG, "image/png")},
    )
    assert note.status_code == 200, note.text
    internal = next(c for c in note.json()["comments"] if c["is_internal"])
    internal_file = internal["attachments"][0]["id"]

    seen = (await client.get(f"/api/v1/helpdesk/tickets/{tid}", headers=bearer(anita))).json()
    assert seen["viewer"] == "requester"
    assert not any(c["is_internal"] for c in seen["comments"])
    assert "LOP" not in str(seen)
    assert seen["can_add_internal_note"] is False and seen["can_manage"] is False
    assert len(seen["attachments"]) == 1
    own_file = seen["attachments"][0]["id"]
    download = await client.get(f"/api/v1/helpdesk/attachments/{own_file}", headers=bearer(anita))
    assert download.status_code == 200 and download.content == PDF
    assert download.headers["content-disposition"].startswith("attachment;")
    hidden = await client.get(
        f"/api/v1/helpdesk/attachments/{internal_file}", headers=bearer(anita)
    )
    assert hidden.status_code == 404
    assert (
        await client.get(f"/api/v1/helpdesk/attachments/{internal_file}", headers=bearer(ops))
    ).status_code == 200
    sneaky = await client.post(
        f"/api/v1/helpdesk/tickets/{tid}/comments",
        headers=bearer(anita),
        data={"body": "psst", "is_internal": "true"},
    )
    assert sneaky.status_code == 403
    assert (
        await client.patch(
            f"/api/v1/helpdesk/tickets/{tid}", headers=bearer(anita), json={"status": "resolved"}
        )
    ).status_code == 403

    # Waiting on the employee; their answer puts it back in progress.
    waiting = await client.patch(
        f"/api/v1/helpdesk/tickets/{tid}",
        headers=bearer(ops),
        json={"status": "waiting_on_employee", "priority": "urgent"},
    )
    assert waiting.json()["status"] == "waiting_on_employee"
    assert waiting.json()["priority"] == "urgent"
    answer = await client.post(
        f"/api/v1/helpdesk/tickets/{tid}/comments",
        headers=bearer(anita),
        data={"body": "HDFC, ending 4411."},
    )
    assert answer.json()["status"] == "in_progress"
    assert ops.id in await _events(session, "helpdesk.ticket.replied")

    # Resolve with a note; the employee hears and may then rate, which closes it.
    resolved = await client.patch(
        f"/api/v1/helpdesk/tickets/{tid}",
        headers=bearer(ops),
        json={"status": "resolved", "note": "The LOP was reversed; paid with October."},
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["status"] == "resolved"
    assert resolved.json()["sla_state"] == "met"
    assert await _events(session, "helpdesk.ticket.resolved") == {anita.id}
    view = (await client.get(f"/api/v1/helpdesk/tickets/{tid}", headers=bearer(anita))).json()
    assert view["can_rate"] and view["can_reopen"] and not view["can_reply"]
    rated = await client.post(
        f"/api/v1/helpdesk/tickets/{tid}/rate",
        headers=bearer(anita),
        json={"rating": 5, "comment": "Quick and clear."},
    )
    assert rated.status_code == 200, rated.text
    body = rated.json()
    assert body["status"] == "closed" and body["satisfaction_rating"] == 5
    assert body["can_rate"] is False and body["can_reopen"] is False
    assert (
        await client.post(
            f"/api/v1/helpdesk/tickets/{tid}/rate", headers=bearer(anita), json={"rating": 1}
        )
    ).status_code == 409

    summary = (await client.get("/api/v1/helpdesk/queue/summary", headers=bearer(admin))).json()
    assert summary["open"] == 1  # HD-00002
    assert summary["resolved_this_month"] == 1
    assert summary["first_response_sample"] == 1
    assert summary["avg_satisfaction"] == 5.0

    home = (await client.get("/api/v1/helpdesk/me/summary", headers=bearer(anita))).json()
    assert home["open_count"] == 1 and home["is_agent"] is False
    assert home["latest"][0]["ticket_number"] == "HD-00002"
    assert (await client.get("/api/v1/helpdesk/queue", headers=bearer(anita))).status_code == 403

    audited = await session.scalars(
        select(AuditLog.summary).where(AuditLog.entity_id == uuid.UUID(tid))
    )
    assert any("Raised HD-00001" in s for s in audited.all())


# ═══════════════════════════════════════════════════════════════════════════
# Who sees what
# ═══════════════════════════════════════════════════════════════════════════


async def test_confidential_tickets_and_other_peoples_tickets(
    client, session, people, other_hr_user
) -> None:
    anita, bala, ops, admin, officer = (
        people["anita"],
        people["bala"],
        people["ops"],
        people["admin"],
        people["officer"],
    )
    created = await client.post(
        "/api/v1/helpdesk/categories",
        headers=bearer(admin),
        json={
            "name": "Grievance",
            "is_confidential": True,
            "request_kind": "grievance",
            "owner_user_id": str(officer.id),
            "sla_hours": 72,
        },
    )
    assert created.status_code == 201, created.text
    grievance = created.json()
    general = (
        await client.post(
            "/api/v1/helpdesk/categories",
            headers=bearer(admin),
            json={"name": "IT & access", "request_kind": "it", "sla_hours": 24},
        )
    ).json()

    raised = await _raise(client, anita, grievance["id"], subject="Treatment by my lead")
    assert raised.status_code == 201, raised.text
    tid = raised.json()["id"]
    # Owned category: handed straight to the owner, who is the one told.
    assert raised.json()["assignee_user_id"] == str(officer.id)
    assert await _events(session, "helpdesk.ticket.created") == {officer.id}
    notification = await session.scalar(
        select(Notification).where(Notification.event_key == "helpdesk.ticket.created")
    )
    assert "Treatment" not in (notification.rendered_body or "")

    # HR ops, a colleague and another tenant: a 404, not a 403.
    for outsider in (ops, bala, other_hr_user):
        response = await client.get(f"/api/v1/helpdesk/tickets/{tid}", headers=bearer(outsider))
        assert response.status_code == 404, outsider.full_name
    comment = await client.post(
        f"/api/v1/helpdesk/tickets/{tid}/comments", headers=bearer(bala), data={"body": "hi"}
    )
    assert comment.status_code == 404
    ops_queue = await client.get("/api/v1/helpdesk/queue", headers=bearer(ops))
    assert tid not in {t["id"] for t in ops_queue.json()}
    assert (await client.get("/api/v1/helpdesk/tickets/mine", headers=bearer(bala))).json() == []

    # The owner and HR admins work it.
    officer_queue = await client.get("/api/v1/helpdesk/queue", headers=bearer(officer))
    assert [t["id"] for t in officer_queue.json()] == [tid]
    assert (await client.get(f"/api/v1/helpdesk/tickets/{tid}", headers=bearer(admin))).json()[
        "viewer"
    ] == "agent"
    to_ops = await client.post(
        f"/api/v1/helpdesk/tickets/{tid}/assign",
        headers=bearer(admin),
        json={"assignee_user_id": str(ops.id)},
    )
    assert to_ops.status_code == 422
    reply = await client.post(
        f"/api/v1/helpdesk/tickets/{tid}/comments",
        headers=bearer(officer),
        data={"body": "Thank you for telling us. Can we talk tomorrow?"},
    )
    assert reply.status_code == 200 and reply.json()["viewer"] == "agent"

    # A non-confidential ticket: HR ops sees it, the colleague still does not.
    it = await _raise(client, anita, general["id"], subject="VPN")
    it_id = it.json()["id"]
    assert (
        await client.get(f"/api/v1/helpdesk/tickets/{it_id}", headers=bearer(ops))
    ).status_code == 200
    assert (
        await client.get(f"/api/v1/helpdesk/tickets/{it_id}", headers=bearer(bala))
    ).status_code == 404

    # Raising for somebody else is HR's, not a colleague's.
    for_anita = await _raise(client, bala, general["id"], employee_id=str(anita.employee_id))
    assert for_anita.status_code == 403
    on_behalf = await _raise(client, ops, general["id"], employee_id=str(bala.employee_id))
    assert on_behalf.status_code == 201 and on_behalf.json()["raised_on_behalf"] is True
    assert on_behalf.json()["employee_id"] == str(bala.employee_id)

    # An HR admin's own ticket stays out of their queue and they cannot work it.
    own = await _raise(client, admin, general["id"], subject="My laptop")
    own_id = own.json()["id"]
    admin_queue = await client.get("/api/v1/helpdesk/queue", headers=bearer(admin))
    assert own_id not in {t["id"] for t in admin_queue.json()}
    assert (
        await client.patch(
            f"/api/v1/helpdesk/tickets/{own_id}", headers=bearer(admin), json={"status": "resolved"}
        )
    ).status_code == 403


# ═══════════════════════════════════════════════════════════════════════════
# Reopen, cancel and the seven-day auto-close
# ═══════════════════════════════════════════════════════════════════════════


async def test_reopen_cancel_and_auto_close(client, session, tenant, people) -> None:
    anita, ops, admin = people["anita"], people["ops"], people["admin"]
    category = (
        await client.post(
            "/api/v1/helpdesk/categories",
            headers=bearer(admin),
            json={"name": "Letters & documents", "request_kind": "letter", "sla_hours": 72},
        )
    ).json()
    tid = (await _raise(client, anita, category["id"], subject="Address proof")).json()["id"]

    closing = await client.patch(
        f"/api/v1/helpdesk/tickets/{tid}", headers=bearer(ops), json={"status": "closed"}
    )
    assert closing.status_code == 422  # closing unresolved needs a reason
    await client.patch(
        f"/api/v1/helpdesk/tickets/{tid}",
        headers=bearer(ops),
        json={"status": "resolved", "note": "Letter is in your documents."},
    )
    reopened = await client.post(
        f"/api/v1/helpdesk/tickets/{tid}/reopen",
        headers=bearer(anita),
        json={"reason": "The letter has my old address."},
    )
    assert reopened.status_code == 200, reopened.text
    body = reopened.json()
    assert body["status"] == "open" and body["reopened_count"] == 1
    assert body["resolved_at"] is None
    assert any(c["body"] == "The letter has my old address." for c in body["comments"])

    await client.patch(
        f"/api/v1/helpdesk/tickets/{tid}",
        headers=bearer(ops),
        json={"status": "resolved", "note": "Reissued."},
    )
    # Eight days on, the scheduler closes it and a reopen is refused.
    ticket = await session.get(HelpdeskTicket, uuid.UUID(tid))
    ticket.resolved_at = datetime.now(UTC) - timedelta(days=8)
    await session.commit()
    result = await helpdesk.auto_close_resolved(session, system_actor(tenant.id))
    assert result["closed"] == 1
    await session.refresh(ticket)
    assert ticket.status is HelpdeskTicketStatus.CLOSED
    late = await client.post(
        f"/api/v1/helpdesk/tickets/{tid}/reopen", headers=bearer(anita), json={"reason": "Still"}
    )
    assert late.status_code == 409
    assert "7 days" in late.json()["detail"]
    detail = (await client.get(f"/api/v1/helpdesk/tickets/{tid}", headers=bearer(anita))).json()
    assert detail["comments"][-1]["body"].startswith("Closed automatically")
    assert detail["comments"][-1]["author_name"] is None

    # Cancelled before resolution: final, and nobody replies on it.
    other = (await _raise(client, anita, category["id"], subject="Experience letter")).json()
    cancelled = await client.post(
        f"/api/v1/helpdesk/tickets/{other['id']}/cancel",
        headers=bearer(anita),
        json={"reason": "Found it."},
    )
    assert cancelled.json()["status"] == "cancelled"
    assert (
        await client.post(
            f"/api/v1/helpdesk/tickets/{other['id']}/comments",
            headers=bearer(ops),
            data={"body": "ok"},
        )
    ).status_code == 409
    assert (
        await client.post(
            f"/api/v1/helpdesk/tickets/{other['id']}/cancel", headers=bearer(ops), json={}
        )
    ).status_code == 403

    # A category with tickets cannot be deleted, only deactivated.
    refused = await client.delete(
        f"/api/v1/helpdesk/categories/{category['id']}", headers=bearer(admin)
    )
    assert refused.status_code == 409
    off = await client.patch(
        f"/api/v1/helpdesk/categories/{category['id']}",
        headers=bearer(admin),
        json={"active": False},
    )
    assert off.json()["active"] is False
    blocked = await _raise(client, anita, category["id"])
    assert blocked.status_code == 422
