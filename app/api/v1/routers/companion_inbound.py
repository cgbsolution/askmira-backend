"""Ask Mira from Slack, Teams and email: where those places send their messages.

No AskMira sign-in reaches these: each is checked against the workspace's own
secret for that place before anything is read (companion/inbound.py). The
workspace in the path only says whose secret to check. Slack and Teams are
answered at once and the reply follows in the background; so is email.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.deps import CurrentUser, DbSession, require_roles
from app.db.session import get_session_factory
from app.models.notifications import Channel
from app.models.tenant import Tenant
from app.models.user import UserRole
from app.services.companion import inbound

router = APIRouter(prefix="/companion", tags=["companion"])
logger = logging.getLogger("askmira.companion.inbound")

Factory = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]
ADMIN_ONLY = [Depends(require_roles(UserRole.ADMIN))]
_NOT_FOUND = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


async def _slack(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, event: dict
) -> None:
    try:
        async with factory() as session:
            outcome = await inbound.handle_slack(session, tenant_id, event)
        logger.info("Slack message for workspace %s: %s", tenant_id, outcome)
    except Exception:  # the request has been answered; a failure is logged, not raised
        logger.exception("Ask Mira could not answer a Slack message for %s", tenant_id)


async def _teams(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, activity: dict
) -> None:
    try:
        async with factory() as session:
            outcome = await inbound.handle_teams(session, tenant_id, activity)
        logger.info("Teams message for workspace %s: %s", tenant_id, outcome)
    except Exception:
        logger.exception("Ask Mira could not answer a Teams message for %s", tenant_id)


async def _email(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    fields: dict,
    repeated: frozenset[str],
) -> None:
    try:
        async with factory() as session:
            tenant = await session.get(Tenant, tenant_id)
            if tenant is not None:
                outcome = await inbound.handle_email(session, tenant, fields, repeated=repeated)
                logger.info("Email for workspace %s: %s", tenant_id, outcome)
    except Exception:
        logger.exception("Ask Mira could not answer an email for %s", tenant_id)


@router.post("/inbound/slack/{tenant_id}", response_model=None)
async def slack_events(
    tenant_id: uuid.UUID,
    request: Request,
    background: BackgroundTasks,
    session: DbSession,
    factory: Factory,
) -> Response | dict[str, Any]:
    secret = await inbound.slack_secret(session, tenant_id)
    if secret is None:
        raise _NOT_FOUND
    body = await request.body()
    if not inbound.slack_signature_ok(
        secret,
        request.headers.get("X-Slack-Request-Timestamp", ""),
        body,
        request.headers.get("X-Slack-Signature", ""),
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not signed by Slack.")
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Not JSON.") from exc
    if payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge")}
    # Slack retries when it did not hear back in time; the first delivery is being answered.
    if request.headers.get("X-Slack-Retry-Num"):
        return Response(status_code=status.HTTP_200_OK)
    if payload.get("type") == "event_callback" and isinstance(payload.get("event"), dict):
        background.add_task(_slack, factory, tenant_id, payload["event"])
    return Response(status_code=status.HTTP_200_OK)


@router.post("/inbound/teams/{tenant_id}", response_model=None)
async def teams_messages(
    tenant_id: uuid.UUID,
    request: Request,
    background: BackgroundTasks,
    session: DbSession,
    factory: Factory,
) -> Response:
    row = await inbound.ask_mira_app(session, tenant_id, Channel.TEAMS)
    if row is None or not row.app_id:
        raise _NOT_FOUND
    try:
        activity = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Not JSON.") from exc
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not token or not isinstance(activity, dict):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not from Teams.")
    claims = await inbound.teams_claims(token, row.app_id, str(activity.get("serviceUrl") or ""))
    if claims is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not from Teams.")
    background.add_task(_teams, factory, tenant_id, activity)
    return Response(status_code=status.HTTP_200_OK)


@router.post("/inbound/email/{tenant_id}/{token}", response_model=None)
async def email_inbound(
    tenant_id: uuid.UUID,
    token: str,
    request: Request,
    background: BackgroundTasks,
    session: DbSession,
    factory: Factory,
) -> Response:
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None or not inbound.token_ok(tenant, token):
        raise _NOT_FOUND
    form = await request.form()
    fields: dict[str, str] = {}
    repeated: set[str] = set()
    for key, value in form.multi_items():
        if not isinstance(value, str):
            continue  # attachments are not read
        if key in fields:
            repeated.add(key)
        else:
            fields[key] = value
    background.add_task(_email, factory, tenant_id, fields, frozenset(repeated))
    return Response(status_code=status.HTTP_200_OK)


# ── The admin's switch for email ─────────────────────────────────────────────


class EmailInboundOn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The workspace's inbound mail provider; the saved one when left out.
    provider: Literal["sendgrid", "mailgun"] | None = None
    #: Mailgun's HTTP webhook signing key; kept from last time when left out.
    signing_key: str | None = Field(default=None, max_length=200)


@router.get("/email-inbound", dependencies=ADMIN_ONLY)
async def email_inbound_status(user: CurrentUser, session: DbSession) -> dict[str, Any]:
    return inbound.email_status(await session.get(Tenant, user.tenant_id))


@router.post("/email-inbound", dependencies=ADMIN_ONLY)
async def email_inbound_on(
    user: CurrentUser,
    session: DbSession,
    request: Request,
    body: EmailInboundOn | None = None,
) -> dict[str, Any]:
    """Switch it on with a new secret address (the old one stops working). Shown once."""
    chosen = body or EmailInboundOn()
    return await inbound.set_email(
        session,
        user,
        enabled=True,
        provider=chosen.provider,
        signing_key=chosen.signing_key,
        request=request,
    )


@router.delete("/email-inbound", dependencies=ADMIN_ONLY)
async def email_inbound_off(
    user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    return await inbound.set_email(session, user, enabled=False, request=request)
