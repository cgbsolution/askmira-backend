"""Each workspace's own Ask Mira: what its admin decides.

Kept in the workspace's settings under "companion". Nothing here can widen what
the companion may read: tools still run as the person asking, and a tool the
person's role may not use is never offered, whatever is switched on here.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.models.audit import AuditAction
from app.models.tenant import Tenant
from app.models.user import User
from app.services import audit

__all__ = ["TOOL_GROUPS", "CompanionConfig", "get_config", "group_on", "set_config"]

#: The switches an admin sees, and what each lets the companion do.
TOOL_GROUPS: dict[str, str] = {
    "policy": "Answer policy, leave-rule and holiday questions from what HR published.",
    "own_record": "Tell a person their own leave balance, latest payslip, attendance or manager.",
    "people": "Find colleagues in the directory (admins and sub-admins).",
    "person_records": "Let admins and sub-admins ask about one employee (current salary, leave "
    "balance, attendance, manager or profile), the company's headcount, joiners and exits, and "
    "birthdays, anniversaries and probation ends. Employees are only ever answered about "
    "themselves.",
    "approvals": "Tell a person what is waiting on their approval: leave, attendance, expenses, "
    "payroll, hiring, exits and more. Only what they may decide.",
    "payroll_figures": "Let admins and sub-admins who may view payroll ask for a month's payroll "
    "totals (gross, net, cost, PF, ESI, PT, TDS) and the company's salary bill.",
    "hr_helpdesk": "Show a person their own HR helpdesk tickets, and the queue to the people who "
    "work it.",
    "who_is_out": "Show who is on approved leave (admins and sub-admins).",
    "attendance": "Show attendance summaries (HR only).",
    "org": "List departments and office locations (admins and sub-admins).",
    "reports": "Answer counting and reporting questions with the report builder.",
    "products": "Say whether the interview, expense and helpdesk tools are connected, and open "
    "the ones this workspace has linked.",
    "hiring_data": "Read jobs, candidates and interviews from the linked interview tool "
    "(HR and hiring managers; never AI scores).",
    "expense_data": "Read expense claims from the linked expense tool: a person's own, those "
    "waiting for their approval, and the whole company's for HR.",
    "helpdesk_data": "Read IT tickets and assets from the linked helpdesk: a person's own tickets "
    "and the assets they hold, IT staff's queues, and the company's tickets and asset register "
    "for helpdesk admins.",
}

#: One to three plain words. The name is written into the agent's instructions,
#: so it must not be able to carry a sentence.
_WORD = r"[A-Za-z0-9]+(?:['-][A-Za-z0-9]+)*"
_NAME = re.compile(rf"^{_WORD}(?: {_WORD}){{0,2}}$")


class CompanionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: What people call it: up to three words of letters and digits.
    name: str = Field(default="Mira", min_length=2, max_length=32)
    #: Shown when a conversation starts.
    greeting: str = Field(default="", max_length=300)
    #: What the admin wants it to know about the company: tone, contacts, terms.
    #: Context for the model, never a way around its rules.
    company_notes: str = Field(default="", max_length=2000)
    #: Tool group → on. A group not listed is on.
    tools: dict[str, bool] = Field(default_factory=dict)
    #: Messages one person may send in a day.
    daily_messages_per_person: int = Field(default=60, ge=5, le=1000)
    #: Model tokens the workspace may use in a calendar month; 0 means no cap.
    monthly_token_budget: int = Field(default=0, ge=0, le=1_000_000_000)

    @field_validator("name")
    @classmethod
    def _plain_name(cls, value: str) -> str:
        value = " ".join(value.split())
        if not _NAME.match(value):
            raise ValueError('Use up to three words of letters and digits, like "Mira".')
        return value

    @field_validator("greeting", "company_notes")
    @classmethod
    def _trimmed(cls, value: str) -> str:
        return value.strip()

    @field_validator("tools")
    @classmethod
    def _known_groups(cls, value: dict[str, bool]) -> dict[str, bool]:
        unknown = sorted(set(value) - set(TOOL_GROUPS))
        if unknown:
            raise ValueError(f"Unknown tool group: {', '.join(unknown)}.")
        return value


def group_on(config: CompanionConfig, group: str) -> bool:
    return config.tools.get(group, True)


async def get_config(session: AsyncSession, user: User) -> CompanionConfig:
    tenant = await session.get(Tenant, user.tenant_id)
    raw = ((tenant.settings or {}).get("companion") or {}) if tenant else {}
    return CompanionConfig.model_validate(raw) if raw else CompanionConfig()


async def set_config(
    session: AsyncSession, user: User, payload: CompanionConfig, *, request: Request | None
) -> CompanionConfig:
    if not permissions.is_admin(user):
        raise permissions.forbidden("Only an admin sets up the workspace's Ask Mira.")
    tenant = await session.get(Tenant, user.tenant_id)
    before = await get_config(session, user)
    settings = dict(tenant.settings or {})
    settings["companion"] = payload.model_dump(mode="json")
    tenant.settings = settings
    changed: dict[str, Any] = {
        key: value
        for key, value in payload.model_dump(mode="json").items()
        if before.model_dump(mode="json").get(key) != value
    }
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.UPDATE,
        entity_type="companion_settings",
        entity_id=tenant.id,
        actor=user,
        changes={
            "before": before.model_dump(mode="json"),
            "after": payload.model_dump(mode="json"),
        },
        summary=f"Ask Mira settings changed: {', '.join(sorted(changed)) or 'nothing'}",
        request=request,
    )
    await session.commit()
    return payload
