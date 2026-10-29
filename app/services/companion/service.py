"""Conversations with Ask Mira, and one turn of one.

A turn: today's limit, then one of two ways of answering. With AI usable
(server configured, workspace switched it on, the person agreed, this month's
budget left) the model holds the conversation: it calls tools, reads what they
found, and answers or makes an offer to act (agent.py). Without it the words
are matched to one lookup by rule (direct.py) and nothing leaves AskMira.
Either way every tool runs here as the person, and a reply is saved beside its
cards. A model turn leaves its DPDP transfer (the categories the model read)
and agent-invocation rows and an AI-action audit row; a rule turn leaves an
audit row.

Offers the person confirms with a button come through `confirm_offer`; a "yes"
in the chat comes through the model's confirm_action tool. Both run the
module's own service as the person (actions.py).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.core.deps import tenant_scoped
from app.models.audit import AuditAction
from app.models.companion import CompanionConversation, CompanionMessage
from app.models.grounding import AgentInvocation, InvocationOutcome
from app.models.tenant import Tenant
from app.models.user import User
from app.services import audit
from app.services.ai import client, governance
from app.services.companion import actions, agent, direct, views
from app.services.companion.config import (
    TOOL_GROUPS,
    CompanionConfig,
    get_config,
    group_on,
)
from app.services.companion.tools import TOOLS, offered, run
from app.services.dpdp import transfers as dpdp_transfers
from app.services.grounding import injection
from app.services.grounding import service as grounding

__all__ = [
    "MessageIn",
    "cancel_offer",
    "confirm_offer",
    "delete_conversation",
    "get_conversation",
    "list_conversations",
    "send",
    "settings_view",
    "status_for",
]

AGENT_KEY = "companion"
#: Model use counted against the workspace's monthly budget: the companion and
#: the two features it hands questions to.
BUDGET_AGENTS = (AGENT_KEY, "assistant", "analytics_questions")
#: Saved messages read back for the model; agent.py keeps the last ten turns of them.
CONTEXT_MESSAGES = 60
IST = ZoneInfo("Asia/Kolkata")


class PickIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: str = Field(min_length=1, max_length=64)
    inputs: dict[str, str] = Field(default_factory=dict, max_length=8)

    @field_validator("inputs")
    @classmethod
    def _short(cls, value: dict[str, str]) -> dict[str, str]:
        if any(len(k) > 32 or len(v) > 100 for k, v in value.items()):
            raise ValueError("That choice is not valid.")
        return value


class MessageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=2000)
    conversation_id: uuid.UUID | None = None
    #: A choice clicked on an earlier answer (a person picked from a list): the
    #: tool and inputs it carries. Run without planning, as the person, and only
    #: when that tool is offered to them now.
    pick: PickIn | None = None

    @field_validator("text")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Write a message.")
        return value.strip()


def _today() -> date:
    return datetime.now(IST).date()


def _start_of(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=IST).astimezone(UTC)


# ── Reading ──────────────────────────────────────────────────────────────────


def _conversation_read(row: CompanionConversation) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "title": row.title,
        "last_message_at": row.last_message_at.isoformat(),
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _message_read(
    row: CompanionMessage, cards: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    steps = [
        {"tool": s.get("tool"), "title": s.get("title")}
        for s in (row.steps or [])
        if isinstance(s, dict)
    ]
    return {
        "id": str(row.id),
        "role": row.role,
        "text": row.text,
        "tool": row.tool,
        "tool_title": TOOLS[row.tool].title if row.tool in TOOLS else None,
        "arguments": row.arguments,
        "steps": steps,
        "cards": cards if cards is not None else (row.cards or []),
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


async def _fresh(session: AsyncSession, user: User, row: CompanionMessage) -> dict[str, Any]:
    """A message as read now: each offer on it shows what became of it."""
    cards = await actions.refresh_cards(session, user, row.cards or [], owner_id=user.id)
    return _message_read(row, cards)


async def _own_conversation(
    session: AsyncSession, user: User, conversation_id: uuid.UUID, *, owner_id: uuid.UUID
) -> CompanionConversation:
    """A conversation of `owner_id` (always the caller). Anyone else's is 'not found'."""
    row = await session.scalar(
        tenant_scoped(
            select(CompanionConversation).where(
                CompanionConversation.id == conversation_id,
                CompanionConversation.user_id == owner_id,
            ),
            CompanionConversation,
            user,
        )
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such conversation.")
    return row


async def list_conversations(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    rows = (
        await session.scalars(
            tenant_scoped(
                select(CompanionConversation)
                .where(CompanionConversation.user_id == user.id)
                .order_by(CompanionConversation.last_message_at.desc())
                .limit(50),
                CompanionConversation,
                user,
            )
        )
    ).all()
    return [_conversation_read(row) for row in rows]


async def _messages(
    session: AsyncSession,
    user: User,
    conversation_id: uuid.UUID,
    *,
    owner_id: uuid.UUID,
    limit: int,
) -> list[CompanionMessage]:
    rows = (
        await session.scalars(
            tenant_scoped(
                select(CompanionMessage)
                .where(
                    CompanionMessage.conversation_id == conversation_id,
                    CompanionMessage.user_id == owner_id,
                )
                # A turn's message and reply share one transaction, so one timestamp:
                # among equals the reply sorts first here, and last once reversed.
                .order_by(CompanionMessage.created_at.desc(), CompanionMessage.role.asc())
                .limit(limit),
                CompanionMessage,
                user,
            )
        )
    ).all()
    return list(reversed(rows))


async def get_conversation(
    session: AsyncSession, user: User, conversation_id: uuid.UUID
) -> dict[str, Any]:
    row = await _own_conversation(session, user, conversation_id, owner_id=user.id)
    messages = await _messages(session, user, row.id, owner_id=user.id, limit=200)
    return {
        **_conversation_read(row),
        "messages": [await _fresh(session, user, m) for m in messages],
    }


async def delete_conversation(
    session: AsyncSession, user: User, conversation_id: uuid.UUID, *, request: Request | None
) -> None:
    row = await _own_conversation(session, user, conversation_id, owner_id=user.id)
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.DELETE,
        entity_type="companion_conversation",
        entity_id=row.id,
        actor=user,
        summary="Ask Mira conversation deleted by its owner",
        request=request,
    )
    await session.delete(row)
    await session.commit()


# ── Limits ───────────────────────────────────────────────────────────────────


async def _sent_today(session: AsyncSession, user: User, *, owner_id: uuid.UUID) -> int:
    count = await session.scalar(
        tenant_scoped(
            select(func.count(CompanionMessage.id)).where(
                CompanionMessage.user_id == owner_id,
                CompanionMessage.role == "user",
                CompanionMessage.created_at >= _start_of(_today()),
            ),
            CompanionMessage,
            user,
        )
    )
    return int(count or 0)


async def _tokens_this_month(session: AsyncSession, user: User) -> int:
    tokens = await session.scalar(
        tenant_scoped(
            select(func.coalesce(func.sum(AgentInvocation.tokens), 0)).where(
                AgentInvocation.agent_key.in_(BUDGET_AGENTS),
                AgentInvocation.created_at >= _start_of(_today().replace(day=1)),
            ),
            AgentInvocation,
            user,
        )
    )
    return int(tokens or 0)


async def _check_limits(
    session: AsyncSession, user: User, config: CompanionConfig, *, ai: bool
) -> None:
    if await _sent_today(session, user, owner_id=user.id) >= config.daily_messages_per_person:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"You have sent today's {config.daily_messages_per_person} messages to "
                f"{config.name}. The count starts again at midnight."
            ),
        )
    if (
        ai
        and config.monthly_token_budget
        and (await _tokens_this_month(session, user) >= config.monthly_token_budget)
    ):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"Your organisation has used this month's allowance for {config.name}. "
                "An admin can raise it in Ask Mira settings."
            ),
        )


# ── Status and settings ──────────────────────────────────────────────────────


async def _reason(session: AsyncSession, user: User) -> str | None:
    """Why a model may not plan this person's turns, or None when it may."""
    ai = await governance.status_for(session, user)
    if not ai["configured"]:
        return "not_configured"
    if not ai["features"].get(AGENT_KEY):
        return "disabled"
    if not ai["my_consent"]:
        return "no_consent"
    return None


async def status_for(session: AsyncSession, user: User) -> dict[str, Any]:
    """What the chat screen needs: which planner answers, why, and who it is.

    The chat is always there. Without AI it answers by matching words to a
    lookup (direct mode); `reason` says why AI is not planning.
    """
    config = await get_config(session, user)
    reason = await _reason(session, user)
    tools = await offered(session, user, config, ai=reason is None)
    return {
        "available": True,
        "mode": "ai" if reason is None else "direct",
        "reason": reason,
        "name": config.name,
        "greeting": config.greeting,
        "tools": [{"key": tool.key, "title": tool.title} for tool in tools],
        "examples": direct.examples(tools),
        "daily_limit": config.daily_messages_per_person,
        "sent_today": await _sent_today(session, user, owner_id=user.id),
        "can_configure": permissions.is_admin(user),
    }


async def settings_view(session: AsyncSession, user: User) -> dict[str, Any]:
    """The admin's view: the workspace's Ask Mira, its switches, and this month's use."""
    if not permissions.is_admin(user):
        raise permissions.forbidden("Only an admin sets up the workspace's Ask Mira.")
    config = await get_config(session, user)
    replies = await session.scalar(
        tenant_scoped(
            select(func.count(CompanionMessage.id)).where(
                CompanionMessage.role == "assistant",
                CompanionMessage.created_at >= _start_of(_today().replace(day=1)),
            ),
            CompanionMessage,
            user,
        )
    )
    ai = await governance.get_settings(session, user)
    return {
        "config": config.model_dump(mode="json"),
        "groups": [
            {"key": key, "description": text, "on": group_on(config, key)}
            for key, text in TOOL_GROUPS.items()
        ],
        "ai_enabled": ai.enabled and ai.companion,
        "usage": {
            "replies_this_month": int(replies or 0),
            "tokens_this_month": await _tokens_this_month(session, user),
        },
    }


# ── A turn ───────────────────────────────────────────────────────────────────


async def send(
    session: AsyncSession, user: User, payload: MessageIn, *, request: Request | None
) -> dict[str, Any]:
    config = await get_config(session, user)
    ai = await _reason(session, user) is None
    await _check_limits(session, user, config, ai=ai)

    now = datetime.now(UTC)
    if payload.conversation_id:
        conversation = await _own_conversation(
            session, user, payload.conversation_id, owner_id=user.id
        )
        history = await _messages(
            session, user, conversation.id, owner_id=user.id, limit=CONTEXT_MESSAGES
        )
    else:
        title = payload.text if len(payload.text) <= 80 else payload.text[:77].rstrip() + "…"
        conversation = CompanionConversation(
            tenant_id=user.tenant_id, user_id=user.id, title=title, last_message_at=now
        )
        session.add(conversation)
        await session.flush()
        history = []

    mine = CompanionMessage(
        tenant_id=user.tenant_id,
        conversation_id=conversation.id,
        user_id=user.id,
        role="user",
        text=payload.text,
        cards=[],
    )
    session.add(mine)
    await session.flush()

    if payload.pick is not None or not ai:
        return await _send_direct(
            session,
            user,
            config,
            conversation,
            mine,
            payload.text,
            request=request,
            pick=payload.pick,
        )

    return await _send_ai(session, user, config, conversation, mine, history, payload, now, request)


async def _send_ai(
    session: AsyncSession,
    user: User,
    config: CompanionConfig,
    conversation: CompanionConversation,
    mine: CompanionMessage,
    history: list[CompanionMessage],
    payload: MessageIn,
    now: datetime,
    request: Request | None,
) -> dict[str, Any]:
    """A turn the model works out with tools (agent.py)."""
    tenant = await session.get(Tenant, user.tenant_id)
    company = tenant.name if tenant else "the company"
    tools = await offered(session, user, config)
    system = agent.system_prompt(config, company, user)
    today = _today()
    offers = await actions.recent_offers(session, user, conversation.id, owner_id=user.id)
    sent = agent.user_content(payload.text, today=today, offers=offers)
    mine.transcript = sent
    await session.flush()
    attempts = injection.detect(payload.text, field="companion_message")
    ctx = actions.ToolContext(
        conversation_id=conversation.id,
        turn_started=now,
        latest_text=payload.text,
        request=request,
    )
    carried = agent.history_categories(history)

    def transfer(categories: set[str], principals: int) -> dict[str, Any]:
        return {
            "agent_key": AGENT_KEY,
            "purpose_key": "ai_assistance",
            "categories": tuple(sorted(categories)),
            "principal_count": principals,
            "description": (
                "A person's message to Ask Mira, the conversation so far, the tools they may "
                "use, and the trimmed results of the lookups the model was allowed to read."
            ),
            "system": system,
            "question": payload.text,
            "started": now,
        }

    try:
        outcome = await agent.run_turn(
            session,
            user,
            config,
            tools,
            system=system,
            history=agent.history_messages(history),
            sent=sent,
            ctx=ctx,
            today=today,
            carried=carried,
        )
    except (client.AiUnavailableError, client.AiDeclinedError) as exc:
        failure = await governance.record_failure(
            session, user, exc, **transfer(set(agent.BASE_CATEGORIES) | carried, 1)
        )
        return await _save_notice(session, user, conversation, mine, str(failure.detail))
    except dpdp_transfers.TransferError as exc:
        return await _save_notice(session, user, conversation, mine, str(exc))

    invocation = await governance.record_call(
        session,
        user,
        completion=outcome.completion(),
        outcome=InvocationOutcome.ANSWERED,
        rendered=outcome.text,
        raw_output=views.as_json(outcome.transcript),
        **transfer(outcome.categories, outcome.principals),
    )
    if attempts:
        await grounding.log_injection_attempts(
            session,
            user,
            attempts=attempts,
            source_table="companion_message",
            invocation_id=invocation.id,
            commit=False,
        )

    cards = list(outcome.cards)
    if outcome.failure is not None:
        cards.append(
            {
                "type": "notice",
                "tone": "warning",
                "text": f"{config.name} stopped part-way: {outcome.failure}",
            }
        )
    last = outcome.steps[-1] if outcome.steps else None
    reply = CompanionMessage(
        tenant_id=user.tenant_id,
        conversation_id=conversation.id,
        user_id=user.id,
        role="assistant",
        text=outcome.text.strip(),
        tool=last["tool"] if last else None,
        arguments=last["inputs"] or None if last else None,
        cards=cards,
        steps=outcome.steps,
        transcript=outcome.transcript,
        invocation_id=invocation.id,
    )
    session.add(reply)
    conversation.last_message_at = datetime.now(UTC)
    await session.flush()
    used = [step["tool"] for step in outcome.steps]
    await audit.record_ai_action(
        session,
        tenant_id=user.tenant_id,
        entity_type="companion_message",
        entity_id=reply.id,
        summary=(
            f"Ask Mira answered {user.full_name}"
            + (f" using {', '.join(dict.fromkeys(used))}" if used else " without a tool")
        ),
        model=outcome.model or client.model_name(),
        actor=user,
        tool=last["tool"] if last else None,
        arguments={"steps": outcome.steps, "model_calls": outcome.calls},
        invocation_id=str(invocation.id),
    )
    await session.commit()
    return {
        "conversation": _conversation_read(conversation),
        "messages": [_message_read(mine), await _fresh(session, user, reply)],
    }


async def _save_notice(
    session: AsyncSession,
    user: User,
    conversation: CompanionConversation,
    mine: CompanionMessage,
    text: str,
) -> dict[str, Any]:
    reply = CompanionMessage(
        tenant_id=user.tenant_id,
        conversation_id=conversation.id,
        user_id=user.id,
        role="assistant",
        text="",
        cards=[{"type": "notice", "tone": "warning", "text": text}],
    )
    session.add(reply)
    conversation.last_message_at = datetime.now(UTC)
    await session.commit()
    return {
        "conversation": _conversation_read(conversation),
        "messages": [_message_read(mine), _message_read(reply)],
    }


async def _send_direct(
    session: AsyncSession,
    user: User,
    config: CompanionConfig,
    conversation: CompanionConversation,
    mine: CompanionMessage,
    text: str,
    *,
    request: Request | None,
    pick: PickIn | None = None,
) -> dict[str, Any]:
    """A turn planned by rule (direct.py), or a choice clicked on an earlier answer.

    No model, nothing sent outside AskMira.
    """
    tools = await offered(session, user, config, ai=False)
    if pick is not None:
        tool = TOOLS.get(pick.tool)
        if tool is None or tool.key not in {t.key for t in tools}:
            plan = direct.DirectPlan(reply="That choice is no longer available to you.")
        else:
            readable = {k: v for k, v in pick.inputs.items() if k in tool.inputs}
            plan = direct.DirectPlan(reply="", tool=tool.key, inputs=readable)
    else:
        plan = direct.plan(
            text, tools, today=_today(), name=config.name, hr=permissions.is_hr(user)
        )
    # The plan only ever names an offered tool; checked again all the same.
    chosen = {tool.key: tool for tool in tools}.get(plan.tool or "")
    cards: list[dict[str, Any]] = []
    used: dict[str, Any] = {}
    steps: list[dict[str, Any]] = []
    shown = plan.reply or "Here is what I found."
    if chosen is not None:
        cards, used = await run(session, user, chosen, plan.inputs, today=_today())
        # Kept for a later turn the model answers: what was shown, as far as it may read it.
        read = agent.readable(chosen, config)
        steps = [{"tool": chosen.key, "title": chosen.title, "inputs": used, "read": read}]
        shown += "\n\n[What AskMira showed the person:]\n" + views.view_of(cards, allowed=read)

    reply = CompanionMessage(
        tenant_id=user.tenant_id,
        conversation_id=conversation.id,
        user_id=user.id,
        role="assistant",
        text=plan.reply,
        tool=chosen.key if chosen else None,
        arguments=used or None,
        cards=cards,
        steps=steps or None,
        transcript=[{"role": "assistant", "content": [{"type": "text", "text": shown}]}],
    )
    session.add(reply)
    conversation.last_message_at = datetime.now(UTC)
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.CREATE,
        entity_type="companion_message",
        entity_id=reply.id,
        actor=user,
        changes={
            "mode": "pick" if pick is not None else "direct",
            "tool": chosen.key if chosen else None,
            "arguments": used,
        },
        summary=(
            f"Ask Mira (direct) answered {user.full_name}"
            + (f" with {chosen.title.lower()}" if chosen else " without a tool")
        ),
        request=request,
    )
    await session.commit()
    return {
        "conversation": _conversation_read(conversation),
        "messages": [_message_read(mine), _message_read(reply)],
    }


# ── Offers decided with a button ─────────────────────────────────────────────


async def _decided(
    session: AsyncSession,
    user: User,
    action: Any,
    text: str,
) -> dict[str, Any]:
    """Note the outcome in the conversation, so the next turn knows it too."""
    conversation = await _own_conversation(session, user, action.conversation_id, owner_id=user.id)
    card = actions.card_for(action)
    reply = CompanionMessage(
        tenant_id=user.tenant_id,
        conversation_id=conversation.id,
        user_id=user.id,
        role="assistant",
        text=text,
        cards=[card],
        transcript=[
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": f"(The person pressed a button on offer {action.id}.) {text}",
                    }
                ],
            }
        ],
    )
    session.add(reply)
    conversation.last_message_at = datetime.now(UTC)
    await session.commit()
    return {"action": card, "message": _message_read(reply)}


async def confirm_offer(
    session: AsyncSession, user: User, action_id: uuid.UUID, *, request: Request | None
) -> dict[str, Any]:
    """The person pressed Confirm: carry the offer out through the module's own service."""
    action = await actions.confirm(
        session, user, action_id, owner_id=user.id, via="button", request=request
    )
    result = action.result or {}
    text = str(result.get("text") or ("Done." if action.status.value == "done" else ""))
    if action.status.value == "failed":
        text = f"That could not be done: {text}"
    return await _decided(session, user, action, text)


async def cancel_offer(
    session: AsyncSession, user: User, action_id: uuid.UUID, *, request: Request | None
) -> dict[str, Any]:
    """The person pressed Cancel: the offer stays in the chat, marked cancelled."""
    action = await actions.cancel(
        session, user, action_id, owner_id=user.id, via="button", request=request
    )
    return await _decided(session, user, action, "Cancelled. Nothing was sent.")
