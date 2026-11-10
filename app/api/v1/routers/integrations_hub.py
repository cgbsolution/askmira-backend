"""Settings → Integrations: the catalogue, and the connections a workspace makes itself."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, ConfigDict, Field

from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.user import UserRole
from app.services.connections import catalogue, zoho_books
from app.services.connections import digio as digio_connection
from app.services.notifications import whatsapp_templates

router = APIRouter(
    prefix="/integrations",
    tags=["integrations"],
    dependencies=[Depends(require_roles(UserRole.ADMIN))],
)


class DigioIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    environment: str = Field(min_length=1, max_length=16)
    client_id: str = Field(min_length=1, max_length=200)
    #: Blank on an existing connection keeps the stored secret.
    client_secret: str | None = Field(default=None, max_length=500)
    webhook_secret: str | None = Field(default=None, max_length=500)


class ZohoBooksIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data_centre: str = Field(min_length=1, max_length=16)
    client_id: str = Field(min_length=1, max_length=200)
    client_secret: str = Field(min_length=1, max_length=500)
    #: The one-time code generated in the Zoho API Console (Self Client).
    code: str = Field(min_length=1, max_length=500)


class ZohoOrganisationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    organisation_id: str = Field(min_length=1, max_length=64)


@router.get("/catalogue")
async def get_catalogue(user: CurrentUser, session: DbSession) -> list[dict[str, Any]]:
    """Every integration, how it works, and where this workspace stands with it."""
    return await catalogue.catalogue(session, user)


@router.get("/digio")
async def get_digio(user: CurrentUser, session: DbSession) -> dict[str, Any]:
    return await digio_connection.status_for(session, user)


@router.put("/digio")
async def put_digio(
    payload: DigioIn, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    """Check the credentials at Digio, then keep them for this workspace."""
    result = await digio_connection.connect(
        session,
        user,
        environment=payload.environment,
        client_id=payload.client_id,
        client_secret=payload.client_secret,
        webhook_secret=payload.webhook_secret,
        request=request,
    )
    await session.commit()
    return result


@router.delete("/digio", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_digio(user: CurrentUser, session: DbSession, request: Request) -> None:
    await digio_connection.disconnect(session, user, request=request)
    await session.commit()


@router.get("/zoho-books")
async def get_zoho_books(user: CurrentUser, session: DbSession) -> dict[str, Any]:
    return await zoho_books.status_for(session, user)


@router.put("/zoho-books")
async def put_zoho_books(
    payload: ZohoBooksIn, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    """Swap the one-time code for a refresh token at Zoho, then keep it for this workspace."""
    result = await zoho_books.connect(
        session,
        user,
        data_centre=payload.data_centre,
        client_id=payload.client_id,
        client_secret=payload.client_secret,
        code=payload.code,
        request=request,
    )
    await session.commit()
    return result


@router.patch("/zoho-books/organisation")
async def choose_zoho_organisation(
    payload: ZohoOrganisationIn, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    result = await zoho_books.choose_organisation(
        session, user, payload.organisation_id, request=request
    )
    await session.commit()
    return result


@router.delete("/zoho-books", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_zoho_books(user: CurrentUser, session: DbSession, request: Request) -> None:
    await zoho_books.disconnect(session, user, request=request)
    await session.commit()


# ── WhatsApp templates ──────────────────────────────────────────────────────


class WhatsAppTemplateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    meta_name: str = Field(min_length=1, max_length=512)
    locale: str = Field(default="en", min_length=2, max_length=16)
    category: str = Field(default="UTILITY", max_length=32)
    parameter_names: list[str] = Field(default_factory=list, max_length=20)
    approved: bool = False


class WhatsAppTemplatePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved: bool | None = None
    rejection_reason: str | None = Field(default=None, max_length=500)
    parameter_names: list[str] | None = Field(default=None, max_length=20)


class WhatsAppLinkIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_id: uuid.UUID | None = None


class WhatsAppTestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phone: str = Field(min_length=6, max_length=32)
    values: list[str] = Field(default_factory=list, max_length=20)


@router.get("/whatsapp/templates")
async def get_whatsapp_templates(user: CurrentUser, session: DbSession) -> dict[str, Any]:
    return await whatsapp_templates.overview(session, user)


@router.post("/whatsapp/templates", status_code=status.HTTP_201_CREATED)
async def post_whatsapp_template(
    payload: WhatsAppTemplateIn, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    return await whatsapp_templates.create_template(
        session, user, **payload.model_dump(), request=request
    )


@router.patch("/whatsapp/templates/{template_id}")
async def patch_whatsapp_template(
    template_id: uuid.UUID,
    payload: WhatsAppTemplatePatch,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    return await whatsapp_templates.update_template(
        session, user, template_id, **payload.model_dump(), request=request
    )


@router.delete(
    "/whatsapp/templates/{template_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def delete_whatsapp_template(
    template_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> None:
    await whatsapp_templates.delete_template(session, user, template_id, request=request)


@router.put("/whatsapp/events/{event_key}")
async def put_whatsapp_event(
    event_key: str, payload: WhatsAppLinkIn, user: CurrentUser, session: DbSession, request: Request
) -> dict[str, Any]:
    return await whatsapp_templates.link_event(
        session, user, event_key[:128], payload.template_id, request=request
    )


@router.post("/whatsapp/templates/{template_id}/test")
async def test_whatsapp_template(
    template_id: uuid.UUID,
    payload: WhatsAppTestIn,
    user: CurrentUser,
    session: DbSession,
    request: Request,
) -> dict[str, Any]:
    return await whatsapp_templates.send_test(
        session, user, template_id, phone=payload.phone, values=payload.values, request=request
    )
