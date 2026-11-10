"""Ask Mira from Slack, Microsoft Teams and email: the part they share.

A message arrives in one of those places from someone the place vouches for
(Slack's signed event, Microsoft's signed activity, an email its own domain
vouched for). Each inbound module checks that first. Here:

* the person is the AskMira login with that work email in that workspace,
  active, in a workspace that is not suspended — nobody else is ever answered,
  and nothing says whether an address exists. Whom the workspace's two-step
  rule covers, or whose role is held to its allowed networks, is asked to use
  AskMira itself: neither can be proved from a chat or a mail server. The rest
  is what a signed-in request carries, including a sub-admin's department or
  location scope;
* the message goes to Ask Mira exactly as if typed in the app
  (companion.send): the same consent, AI switch, daily limit, tools, rights and
  audit. Consecutive messages in one chat continue one conversation (recent
  conversations keep their place's label in the title);
* the reply is rendered as plain text for the place: the answer, then each
  card in a line or a few — tables cut to their first rows, offers with their
  figures and how to confirm. A card from a lookup of pay, leave, attendance or
  other personal records is not written out: it says the result is in AskMira.
  Slack and Teams confirm by the person replying "yes" (Ask Mira's own typed-yes
  check: the offer must be from an earlier message of that conversation); email
  never confirms by reply — the tools are not offered and a confirm is refused —
  it links to AskMira, because an email can be forwarded or forged.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import has_direct_reports, tenant_filter
from app.models.companion import CompanionConversation
from app.models.tenant import Tenant
from app.models.user import User

__all__ = [
    "CARRIED",
    "LABELS",
    "Acting",
    "Reply",
    "acting_person",
    "answer",
    "login_by_email",
    "render",
]

LABELS = {"slack": "Slack", "teams": "Teams", "email": "Email"}
#: A chat that goes quiet this long starts a new conversation.
CONTINUES_FOR = timedelta(hours=12)
TABLE_ROWS = 10
#: What a reply in Slack, Teams or an email body may carry (DPDP categories, see the
#: "chat_apps" and email sub-processors): who people are and business records. A card
#: from a lookup that reads anything else (pay, leave, attendance…) stays in AskMira.
CARRIED = frozenset({"identity", "organisational", "ai_interaction", "communication"})


@dataclass(slots=True)
class Reply:
    text: str
    conversation_id: uuid.UUID | None = None
    offers: int = 0


@dataclass(slots=True)
class Acting:
    """Who a message from a chat or an email is answered as, or why not."""

    #: The login to answer as; None when nobody is.
    user: User | None = None
    #: The login with that email, also when refused (so an email can say why).
    login: User | None = None
    #: Said instead of an answer; None means the generic "only AskMira logins".
    refusal: str | None = None
    suspended: bool = False


async def login_by_email(session: AsyncSession, tenant_id: uuid.UUID, email: str) -> User | None:
    """The workspace's active login with this email, or None."""
    email = (email or "").strip().lower()
    if "@" not in email:
        return None
    return await session.scalar(
        select(User).where(
            User.tenant_id == tenant_id,
            func.lower(User.email) == email,
            User.is_active.is_(True),
        )
    )


async def acting_person(session: AsyncSession, tenant_id: uuid.UUID, email: str) -> Acting:
    """The login to answer as, with what a signed-in request would carry; or why not.

    The same refusals a portal request meets (deps.get_current_user): a suspended
    or deleted workspace, and the workspace's session policy. Then the person's
    direct reports and a sub-admin's department or location scope, as for a
    message typed in Ask Mira.
    """
    from app.core.access_control import load_request_access
    from app.models.opsec import TenantSessionPolicy
    from app.services.companion import actions
    from app.services.opsec.enforcement import PRIVILEGED_ROLES, two_step_applies

    tenant = await session.get(Tenant, tenant_id)
    if tenant is None or not tenant.is_active or tenant.deleted_at is not None:
        return Acting(
            refusal="Ask Mira isn't available for your company right now.", suspended=True
        )
    user = await login_by_email(session, tenant_id, email)
    if user is None:
        return Acting()
    policy = await session.scalar(
        select(TenantSessionPolicy).where(tenant_filter(TenantSessionPolicy, tenant_id))
    )
    if two_step_applies(policy, user):
        return Acting(
            login=user,
            refusal=(
                "Your company requires two-step sign-in, which a chat or an email can't prove, "
                "so ask me in AskMira itself."
            ),
        )
    if (
        policy is not None
        and policy.enforce_ip_allowlist_for_privileged
        and tuple(policy.ip_allowlist or ())
        and user.role in PRIVILEGED_ROLES
    ):
        return Acting(
            login=user,
            refusal=(
                "Your role can only be used from your company's allowed networks, so ask me in "
                "AskMira itself."
            ),
        )
    user.has_reports = await has_direct_reports(session, user)
    await load_request_access(actions._scope_for(None, actions._HOME_PATH), session, user)
    return Acting(user=user, login=user)


def _portal(path: str) -> str:
    return f"{(settings.WEB_APP_URL or '').rstrip('/')}{path}"


async def _conversation(
    session: AsyncSession, user: User, place: str, thread: str
) -> uuid.UUID | None:
    """The conversation this chat continues, if it spoke in the last few hours."""
    since = datetime.now(UTC) - CONTINUES_FOR
    return await session.scalar(
        select(CompanionConversation.id)
        .where(
            CompanionConversation.tenant_id == user.tenant_id,
            CompanionConversation.user_id == user.id,
            CompanionConversation.title.like(f"[{LABELS[place]} {thread[:24]}]%"),
            CompanionConversation.last_message_at >= since,
        )
        .order_by(CompanionConversation.last_message_at.desc())
        .limit(1)
    )


def _cell(value: Any) -> str:
    return "—" if value in (None, "") else str(value)


def carried(card: dict[str, Any]) -> bool:
    """Whether this card may be written out in a chat or an email.

    AskMira's own notices and the offers the person asked for are (an offer must
    say what it will do for a reply to mean anything, as an approval email does).
    A lookup's record or table only when its tool reads nothing beyond CARRIED.
    """
    from app.services.companion.tools import TOOLS

    if card.get("type") in ("notice", "proposal"):
        return True
    tool = TOOLS.get(str(card.get("from_tool") or ""))
    return tool is None or set(tool.reads) <= CARRIED


def render(text: str, cards: list[dict[str, Any]], *, place: str, conversation_id: Any) -> str:
    """The answer and its cards as plain text, with how to confirm an offer here."""
    lines = [text.strip()] if text and text.strip() else []
    for card in cards:
        kind = card.get("type")
        if not carried(card):
            title = str(card.get("title") or "The result")
            lines.append(
                f"{title}: it holds personal records, so it is kept in AskMira, not sent "
                f"{'by email' if place == 'email' else 'here'}. Open the conversation to see it."
            )
            continue
        if kind == "notice":
            lines.append(str(card.get("text") or ""))
        elif kind == "table":
            columns = [str(c) for c in card.get("columns") or []]
            rows = card.get("rows") or []
            lines.append(f"{card.get('title') or 'Results'} ({card.get('caption') or ''})".strip())
            if columns:
                lines.append(" · ".join(columns))
            lines += ["• " + " · ".join(_cell(c) for c in row) for row in rows[:TABLE_ROWS]]
            if len(rows) > TABLE_ROWS:
                lines.append(f"…and {len(rows) - TABLE_ROWS} more in AskMira.")
        elif kind == "record":
            lines.append(str(card.get("title") or ""))
            lines += [f"• {f.get('label')}: {f.get('value')}" for f in card.get("figures") or []]
            if card.get("note"):
                lines.append(str(card["note"]))
        elif kind == "proposal":
            lines.append(f"{card.get('title')}")
            lines += [f"• {f.get('label')}: {f.get('value')}" for f in card.get("figures") or []]
            table = card.get("table") or {}
            for row in (table.get("rows") or [])[:TABLE_ROWS]:
                lines.append("• " + " · ".join(_cell(c) for c in row))
            lines += [str(n) for n in card.get("notes") or [] if n]
            status = card.get("status")
            if status == "proposed" and card.get("waiting"):
                lines.append(str(card["waiting"]))
            elif status == "proposed":
                if place == "email":
                    lines.append(
                        f"Nothing is done yet. Confirm it in AskMira: "
                        f"{_portal(f'/ask-mira?c={conversation_id}')}"
                    )
                else:
                    label = card.get("confirm_label") or "Confirm"
                    lines.append(f"Nothing is done yet. Reply “yes” to {label.lower()}, or “no”.")
            elif card.get("result") and (card["result"] or {}).get("text"):
                lines.append(str(card["result"]["text"]))
        elif kind == "policy":
            lines.append(str(card.get("answer") or ""))
            for cite in card.get("citations") or []:
                lines.append(f"• {cite.get('title') or cite.get('document') or ''}".strip())
        elif kind == "chart":
            lines.append(f"{card.get('title')}: open AskMira to see the chart.")
        elif kind in ("launch", "own_record", "connect"):
            target = card.get("href") or card.get("open") or "/settings/connectors"
            words = card.get("text") or card.get("name") or ""
            lines.append(f"{words} {_portal(str(target))}".strip())
    lines.append(f"Open this conversation in AskMira: {_portal(f'/ask-mira?c={conversation_id}')}")
    return "\n".join(line for line in lines if line is not None).strip()


async def _record_transfer(
    session: AsyncSession, user: User, cards: list[dict[str, Any]], *, place: str
) -> None:
    """The reply leaves AskMira for Slack, Teams or the mail relay: in the register."""
    from app.services.companion.tools import TOOLS
    from app.services.dpdp import service as dpdp_service

    categories = {"ai_interaction", "communication"}
    for card in cards:
        tool = TOOLS.get(str(card.get("from_tool") or ""))
        if tool is not None and card.get("type") not in ("notice", "proposal") and carried(card):
            categories |= set(tool.reads) & CARRIED
    await dpdp_service.record_transfer(
        session,
        user,
        sub_processor_key="google_email" if place == "email" else "chat_apps",
        purpose_key="ai_assistance",
        category_keys=tuple(sorted(categories)),
        principal_count=1,
        description=f"Ask Mira's answer to {user.full_name}, sent by {LABELS[place]}.",
    )


async def answer(session: AsyncSession, user: User, text: str, *, place: str, thread: str) -> Reply:
    """Ask Mira, as the person, in this chat's conversation; the reply as plain text."""
    from app.services.companion import service as companion

    words = " ".join((text or "").split())[:2000]
    if not words:
        return Reply(text="Send me a question, and I'll look it up as you in AskMira.")
    conversation_id = await _conversation(session, user, place, thread)
    try:
        turn = await companion.send(
            session,
            user,
            companion.MessageIn(text=words, conversation_id=conversation_id),
            request=None,
            place=place,
        )
    except HTTPException as exc:  # a refusal (daily limit, AI off…): its own words
        detail = exc.detail
        return Reply(text=str(detail) if isinstance(detail, str) else "Ask Mira could not answer.")
    conversation = turn["conversation"]
    new_id = uuid.UUID(str(conversation["id"]))
    if conversation_id is None:
        # Labelled so the next message from this chat continues it.
        row = await session.get(CompanionConversation, new_id)
        if row is not None:
            row.title = f"[{LABELS[place]} {thread[:24]}] {row.title}"[:120]
            await session.commit()
    reply = turn["messages"][1]
    cards = reply.get("cards") or []
    await _record_transfer(session, user, cards, place=place)
    return Reply(
        text=render(reply.get("text") or "", cards, place=place, conversation_id=new_id),
        conversation_id=new_id,
        offers=sum(
            1 for c in cards if c.get("type") == "proposal" and c.get("status") == "proposed"
        ),
    )
