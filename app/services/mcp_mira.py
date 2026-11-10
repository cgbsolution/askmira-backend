"""Ask Miraa's tools over MCP, for a person who connected Claude or ChatGPT.

The MCP server's own tools (mcp.py) read the directory, leave and attendance.
These add Ask Miraa's lookups and actions, for a person connected through OAuth
(never an API key: Ask Miraa acts as a person, with their rights in AskMiraa and in
each linked product), with the scope ``mira:use``.

* **Lookups** are the same code Ask Miraa runs (tools.run), as the person, and
  return the same text Ask Miraa's own model reads (views.view_of) — and only
  for lookups whose results are business records and counts (budgets, deals,
  pipeline, procurement, expense totals, joiner counts, P&L forecast): their
  DPDP categories must be ones the "AI assistants you connect" sub-processor is
  registered to receive, and the people in them never travel by name.
* **Actions** only prepare. The offer is saved in a new Ask Miraa conversation,
  exactly as in the app, and the assistant is given its link: the person opens
  AskMiraa and presses Confirm there. Nothing is changed from outside.

Both need the workspace's AI on and Ask Miraa's tool group on; every call is in
the audit log, and every lookup that answered is in the transfer register.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.audit import AuditAction
from app.models.companion import CompanionConversation, CompanionMessage
from app.models.user import User
from app.services import audit

__all__ = ["PREFIX", "SCOPE", "call", "list_tools"]

SCOPE = "mira:use"
PREFIX = "mira_"
SUB_PROCESSOR = "mcp_client"
#: What a lookup's result may carry to an outside assistant: business records and counts.
_SAFE = frozenset({"organisational", "ai_interaction"})


async def _offered(session: AsyncSession, actor: User) -> list[Any]:
    from app.services.companion import config as companion_config
    from app.services.companion import tools

    config = await companion_config.get_config(session, actor)
    out = []
    for tool in await tools.offered(session, actor, config, ai=True):
        if tool.group == "actions" or tool.feature:
            continue  # confirming happens in AskMiraa; feature tools answer with a model of ours
        if tool.kind == "read" and not (config.ai_reads_results and set(tool.reads) <= _SAFE):
            # The workspace's "Let Miraa read what it looks up" switch holds for an outside
            # assistant too: with it off, no lookup result is handed to one.
            continue
        out.append(tool)
    return out


def _is_person(actor: User) -> bool:
    return getattr(actor, "id", None) is not None and not str(actor.email).endswith(".invalid")


async def list_tools(session: AsyncSession, actor: User, scopes: frozenset[str]) -> list[dict]:
    if SCOPE not in scopes or not _is_person(actor):
        return []
    from app.services.companion import tools as companion_tools

    listed = []
    for tool in await _offered(session, actor):
        schema = companion_tools.tool_schema(tool)
        prepare = tool.kind == "action"
        listed.append(
            {
                "name": PREFIX + tool.key,
                "title": ("Prepare: " if prepare else "") + tool.title,
                "description": tool.description
                + (
                    " Over MCP this only prepares it: the person confirms it in AskMiraa, from "
                    "the link returned. Nothing changes until they do."
                    if prepare
                    else ""
                ),
                "inputSchema": schema["input_schema"],
                "annotations": {
                    "title": tool.title,
                    "readOnlyHint": not prepare,
                    "destructiveHint": False,
                    "openWorldHint": bool(tool.product),
                },
            }
        )
    return listed


def _text(text: str, *, error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": error}


async def call(
    session: AsyncSession, actor: User, scopes: frozenset[str], name: str, arguments: dict
) -> dict[str, Any] | None:
    """Run one Ask Miraa tool for an MCP client; None when `name` is not one of these."""
    if not name.startswith(PREFIX):
        return None
    from app.services.ai import governance
    from app.services.companion import actions, views
    from app.services.companion import tools as companion_tools
    from app.services.companion.service import _today

    if SCOPE not in scopes or not _is_person(actor):
        return _text("Connect as yourself, with Ask Miraa allowed, to use this tool.", error=True)
    if not (await governance.get_settings(session, actor)).enabled:
        return _text(
            "AI features are switched off for this workspace, so an outside AI assistant cannot "
            "use Ask Miraa. An admin can switch them on in Settings → AI.",
            error=True,
        )
    key = name.removeprefix(PREFIX)
    tool = next((t for t in await _offered(session, actor) if t.key == key), None)
    if tool is None:
        return _text(f"Ask Miraa's {key} is not available to you here.", error=True)
    inputs = companion_tools.coerce(tool, arguments)
    now = datetime.now(UTC)

    if tool.kind == "action":
        conversation = CompanionConversation(
            tenant_id=actor.tenant_id,
            user_id=actor.id,
            title=f"Prepared from an AI assistant: {tool.title}"[:80],
            last_message_at=now,
        )
        session.add(conversation)
        await session.flush()
        ctx = actions.ToolContext(conversation_id=conversation.id, turn_started=now, latest_text="")
        cards, used = await companion_tools.run(
            session, actor, tool, inputs, today=_today(), ctx=ctx
        )
        offers = [c for c in cards if c.get("type") == "proposal"]
        if offers:
            session.add(
                CompanionMessage(
                    tenant_id=actor.tenant_id,
                    conversation_id=conversation.id,
                    user_id=actor.id,
                    role="assistant",
                    text="Prepared from your AI assistant. Check it and confirm, or cancel.",
                    cards=cards,
                )
            )
            link = f"{(settings.WEB_APP_URL or '').rstrip('/')}/ask-mira?c={conversation.id}"
            reply = (
                f"Prepared, not done. The person must open {link} and press "
                f"“{offers[0].get('confirm_label') or 'Confirm'}” in AskMiraa; nothing changes "
                "until they do. The offer expires in two hours."
            )
        else:
            # A question or a refusal about what the assistant sent: say it, so it can
            # ask the person. Nothing was prepared, so no conversation is kept.
            await session.delete(conversation)
            reply = " ".join(
                str(c.get("text")) for c in cards if c.get("type") == "notice" and c.get("text")
            ) or views.view_of(cards, allowed=False)
        await _audit(session, actor, key, used, prepared=bool(offers))
        await session.commit()
        return _text(reply)

    cards, used = await companion_tools.run(session, actor, tool, inputs, today=_today())
    view = views.view_of(cards, allowed=True)
    await _audit(session, actor, key, used, prepared=False)
    from app.services.dpdp import service as dpdp_service

    await dpdp_service.record_transfer(
        session,
        actor,
        sub_processor_key=SUB_PROCESSOR,
        purpose_key="ai_assistance",
        category_keys=tuple(sorted(set(tool.reads) - {"ai_interaction"})) or ("organisational",),
        principal_count=0,
        description=f"Ask Miraa's {key} answered an outside AI assistant over MCP.",
        commit=False,
    )
    await session.commit()
    return {
        "content": [{"type": "text", "text": view}],
        "structuredContent": {"tool": key, "inputs": used, "text": view},
        "isError": False,
    }


async def _audit(
    session: AsyncSession, actor: User, key: str, used: dict[str, Any], *, prepared: bool
) -> None:
    await audit.record(
        session,
        tenant_id=actor.tenant_id,
        action=AuditAction.AI_ACTION,
        entity_type="mcp_tool",
        actor_user_id=actor.id,
        actor_type="mcp_agent",
        summary=(f"MCP: Ask Miraa {'prepared' if prepared else 'ran'} {key} for {actor.full_name}"),
        ai_context={"tool": PREFIX + key, "arguments": json.loads(json.dumps(used, default=str))},
    )
