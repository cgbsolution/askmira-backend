"""SCIM 2.0 endpoints (slice 04).

Mounted outside the versioned API prefix, at `/scim/v2`, because SCIM clients
expect that path and several will not let you configure otherwise.

Authentication is a per-tenant bearer token, not a user JWT — the caller is a
directory, not a person. The token establishes the tenant.

**De-provisioning behaviour is documented in `app/services/identity/scim.py`.**
That is the question security reviewers ask, and the short answer is: access is
revoked immediately, employment and payroll history is retained.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Header, Query, Request, Response, status
from fastapi.responses import JSONResponse

from app.core.deps import DbSession
from app.services.identity import scim as scim_service
from app.services.identity.scim import ScimError

router = APIRouter(prefix="/scim/v2", tags=["scim"])

SCIM_MEDIA_TYPE = "application/scim+json"


def _scim_response(body: dict[str, Any], status_code: int = 200) -> JSONResponse:
    return JSONResponse(content=body, status_code=status_code, media_type=SCIM_MEDIA_TYPE)


def _error_response(exc: ScimError) -> JSONResponse:
    """SCIM-shaped errors, not this application's default envelope.

    Directories branch on `scimType`, and a non-SCIM error body makes an
    integration failure very hard to diagnose from the directory's side.
    """
    return _scim_response(
        scim_service.scim_error(exc.status, exc.detail, exc.scim_type), exc.status
    )


@router.get("/ServiceProviderConfig")
async def service_provider_config() -> JSONResponse:
    """Capability advertisement.

    `patch.supported` is true and means it: Azure AD de-provisions with a PATCH
    that replaces `active`, never with a DELETE, so a service that only
    implements DELETE silently never de-provisions anyone.
    """
    return _scim_response(
        {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"],
            "documentationUri": "https://askmira.example/docs/scim",
            "patch": {"supported": True},
            "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
            "filter": {"supported": True, "maxResults": 200},
            "changePassword": {"supported": False},
            "sort": {"supported": False},
            "etag": {"supported": False},
            "authenticationSchemes": [
                {
                    "type": "oauthbearertoken",
                    "name": "OAuth Bearer Token",
                    "description": "A per-workspace bearer token, rotatable from admin settings.",
                }
            ],
        }
    )


@router.get("/Users")
async def list_users(
    session: DbSession,
    authorization: str | None = Header(default=None),
    filter: str | None = Query(default=None),
    startIndex: int = Query(default=1, ge=1),  # noqa: N803 - SCIM casing
    count: int = Query(default=100, ge=1, le=200),
) -> JSONResponse:
    try:
        token = await scim_service.authenticate_token(session, authorization)
        body = await scim_service.list_users(
            session,
            token.tenant_id,
            filter_expression=filter,
            start_index=startIndex,
            count=count,
        )
        await session.commit()
        return _scim_response(body)
    except ScimError as exc:
        return _error_response(exc)


@router.get("/Users/{user_id}")
async def get_user(
    user_id: uuid.UUID, session: DbSession, authorization: str | None = Header(default=None)
) -> JSONResponse:
    try:
        token = await scim_service.authenticate_token(session, authorization)
        user = await scim_service.get_user(session, token.tenant_id, user_id)
        await session.commit()
        return _scim_response(scim_service.to_scim_user(user))
    except ScimError as exc:
        return _error_response(exc)


@router.post("/Users", status_code=status.HTTP_201_CREATED)
async def create_user(
    request: Request, session: DbSession, authorization: str | None = Header(default=None)
) -> JSONResponse:
    """Provision a user.

    Any `roles` attribute in the payload is ignored. A corporate directory
    does not get to decide who is an administrator in AskMira.
    """
    try:
        token = await scim_service.authenticate_token(session, authorization)
        payload = await request.json()
        user = await scim_service.provision_user(session, token.tenant_id, payload)
        return _scim_response(scim_service.to_scim_user(user), status.HTTP_201_CREATED)
    except ScimError as exc:
        return _error_response(exc)


@router.patch("/Users/{user_id}")
async def patch_user(
    user_id: uuid.UUID,
    request: Request,
    session: DbSession,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    """Apply a SCIM PatchOp.

    A patch that sets `active` to false revokes every session immediately and
    retains all employment history. See the service module for why.
    """
    try:
        token = await scim_service.authenticate_token(session, authorization)
        payload = await request.json()
        user = await scim_service.apply_patch(session, token.tenant_id, user_id, payload)
        return _scim_response(scim_service.to_scim_user(user))
    except ScimError as exc:
        return _error_response(exc)


@router.delete("/Users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: uuid.UUID, session: DbSession, authorization: str | None = Header(default=None)
) -> Response:
    """De-provision.

    A SCIM DELETE is an *access* decision, not a records-retention decision.
    It deactivates the login and revokes sessions. It does not delete the
    employee record, payslips, attendance or audit history — those are
    statutory records, and an employee leaving does not entitle anyone to
    destroy the evidence of what they were paid. Erasure is a separate,
    deliberate flow under the retention policy.
    """
    try:
        token = await scim_service.authenticate_token(session, authorization)
        await scim_service.deactivate_user(session, token.tenant_id, user_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    except ScimError as exc:
        return _error_response(exc)


@router.get("/Groups")
async def list_groups(
    session: DbSession, authorization: str | None = Header(default=None)
) -> JSONResponse:
    """Groups are advertised but empty, on purpose.

    A directory group would be a natural place to map roles from, and roles are
    assigned in AskMira. Returning an empty list satisfies clients that probe
    for the endpoint without creating a path from directory membership to
    privilege here.
    """
    try:
        await scim_service.authenticate_token(session, authorization)
        await session.commit()
        return _scim_response(
            {
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:ListResponse"],
                "totalResults": 0,
                "startIndex": 1,
                "itemsPerPage": 0,
                "Resources": [],
            }
        )
    except ScimError as exc:
        return _error_response(exc)
