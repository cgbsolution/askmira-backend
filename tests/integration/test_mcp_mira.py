"""Ask Miraa's tools over MCP, for a person who connected Claude or ChatGPT as themselves.

* With ``mira:use`` the person's own Ask Miraa lookups are listed (mira_*), only
  those whose results carry no one's personal record; an API key gets none.
* A lookup answers with the text Ask Miraa's model reads, is audited and is in
  the transfer register.
* An action only prepares: the offer waits in a new Ask Miraa conversation, the
  assistant gets its link, and nothing changes until the person confirms there.
* With AI switched off nothing is answered.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.companion import CompanionAction, CompanionActionStatus, CompanionConversation
from app.models.dpdp import CrossBorderTransfer
from app.models.hiring import Requisition, RequisitionStatus
from app.models.tenant import Tenant
from app.models.user import User
from app.services import mcp
from app.services.ai import governance
from app.services.companion import service as companion
from tests.integration.conftest import requires_db
from tests.integration.test_companion_requisition import SECOND, hiring, hr  # noqa: F401

pytestmark = requires_db

SCOPES = frozenset({"employees:read", "org:read", "leave:read", "attendance:read", "mira:use"})


async def _rpc(session: AsyncSession, actor: User, method: str, **params: Any) -> dict[str, Any]:
    reply = await mcp.handle(
        session, actor, SCOPES, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    )
    assert reply is not None and "result" in reply, reply
    return reply["result"]


async def _names(session: AsyncSession, actor: User, scopes=SCOPES) -> set[str]:
    reply = await mcp.handle(
        session, actor, scopes, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    )
    return {tool["name"] for tool in reply["result"]["tools"]}


async def test_a_person_gets_their_own_ask_mira_lookups_and_prepare_tools(
    session: AsyncSession,
    tenant: Tenant,
    hr: User,  # noqa: F811
    hiring: dict,  # noqa: F811
) -> None:
    names = await _names(session, hr)
    assert {"mira_department_budget", "mira_raise_requisition"} <= names
    assert "search_employees" in names, "the server's own tools are still there"
    # Results that carry a person's record never go to an outside assistant.
    assert not names & {"mira_employee_record", "mira_my_record", "mira_payroll_summary"}
    assert not names & {"mira_confirm_action", "mira_cancel_action"}
    # Without the scope, none of Ask Miraa's.
    without = await _names(session, hr, SCOPES - {"mira:use"})
    assert not {n for n in without if n.startswith("mira_")}


async def test_an_api_key_gets_none_of_ask_miras_tools(
    session: AsyncSession,
    tenant: Tenant,
    hr: User,  # noqa: F811
) -> None:
    from app.models.user import UserRole

    system = User(
        tenant_id=tenant.id, email="api-key@system.askmira.invalid", full_name="API key x",
        role=UserRole.SUB_ADMIN, is_active=True, password_hash="!",
    )  # fmt: skip
    reply = await mcp.handle(
        session, system, SCOPES, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    )
    assert not [t for t in reply["result"]["tools"] if t["name"].startswith("mira_")]


async def test_a_lookup_answers_as_ask_mira_reads_it_and_is_recorded(
    session: AsyncSession,
    tenant: Tenant,
    hr: User,  # noqa: F811
    hiring: dict,  # noqa: F811
) -> None:
    result = await _rpc(
        session, hr, "tools/call", name="mira_department_budget", arguments={"query": "Sales West"}
    )
    assert result["isError"] is False
    assert "Sales West has no headcount budget" in result["content"][0]["text"]
    audited = await session.scalar(
        select(func.count(AuditLog.id)).where(AuditLog.entity_type == "mcp_tool")
    )
    assert audited == 1
    transfers = (
        await session.scalars(
            select(CrossBorderTransfer).where(CrossBorderTransfer.sub_processor_key == "mcp_client")
        )
    ).all()
    assert len(transfers) == 1


async def test_an_action_is_only_prepared_and_waits_for_the_person_in_askmira(
    session: AsyncSession,
    tenant: Tenant,
    hr: User,  # noqa: F811
    hiring: dict,  # noqa: F811
) -> None:
    result = await _rpc(session, hr, "tools/call", name="mira_raise_requisition", arguments=SECOND)
    text = result["content"][0]["text"]
    assert result["isError"] is False
    assert text.startswith("Prepared, not done.") and "/ask-mira?c=" in text
    assert await session.scalar(select(func.count(Requisition.id))) == 0
    action = await session.scalar(select(CompanionAction))
    assert action.status is CompanionActionStatus.PROPOSED and action.kind == "requisition"
    conversation = await session.get(CompanionConversation, action.conversation_id)
    assert conversation.user_id == hr.id and str(conversation.id) in text

    # The person confirms it in AskMiraa; only then is it raised.
    done = await companion.confirm_offer(session, hr, action.id, request=None)
    assert done["action"]["status"] == "done", done["action"]
    requisition = await session.scalar(select(Requisition))
    assert requisition.status is RequisitionStatus.PENDING_APPROVAL


async def test_a_missing_detail_comes_back_as_a_question(
    session: AsyncSession,
    tenant: Tenant,
    hr: User,  # noqa: F811
    hiring: dict,  # noqa: F811
) -> None:
    asked = {**SECOND, "ctc_max": ""}
    result = await _rpc(session, hr, "tools/call", name="mira_raise_requisition", arguments=asked)
    assert "yearly CTC ceiling" in result["content"][0]["text"]
    assert await session.scalar(select(func.count(CompanionAction.id))) == 0


async def test_nothing_is_answered_with_ai_off(
    session: AsyncSession,
    tenant: Tenant,
    hr: User,  # noqa: F811
    hiring: dict,  # noqa: F811
) -> None:
    await governance.set_settings(session, hr, governance.AiSettings(enabled=False), request=None)
    result = await _rpc(
        session, hr, "tools/call", name="mira_department_budget", arguments={"query": ""}
    )
    assert result["isError"] is True and "switched off" in result["content"][0]["text"]
