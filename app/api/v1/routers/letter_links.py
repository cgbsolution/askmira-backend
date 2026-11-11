"""Read, sign and accept a letter from its email link: no sign-in.

The token in the path names one letter and the person it was issued to. GET
only shows (mail scanners open links); POST accepts, with the typed name and a
signature picture that is printed into the signed copy and not kept. See
app.services.documents.letter_link.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, File, Form, Request, Response, UploadFile

from app.core.deps import DbSession
from app.services.documents import letter_link, signed_copy

router = APIRouter(prefix="/public/letters", tags=["documents"])

_PRIVATE = {"Cache-Control": "private, no-store", "X-Robots-Tag": "noindex"}


@router.get("/{token}")
async def describe(token: str, session: DbSession, request: Request) -> dict[str, Any]:
    """Which letter, for whom, and whether it is waiting to be signed."""
    return await letter_link.describe(session, token, request)


@router.get("/{token}/pages/{number}")
async def page(token: str, number: int, session: DbSession, request: Request) -> Response:
    """One page of the letter, as a picture of exactly what prints."""
    data = await letter_link.page(session, token, number, request)
    return Response(content=data, media_type="image/png", headers=_PRIVATE)


@router.get("/{token}/pdf")
async def pdf(token: str, session: DbSession, request: Request) -> Response:
    """The letter as a PDF: the signed copy once it has been accepted."""
    filename, data = await letter_link.pdf(session, token, request)
    return Response(
        content=data,
        media_type="application/pdf",
        headers={**_PRIVATE, "Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/{token}/acknowledge")
async def acknowledge(
    token: str,
    session: DbSession,
    request: Request,
    typed_name: str = Form(..., max_length=255),
    accept: bool = Form(...),
    signature: UploadFile = File(...),
) -> dict[str, Any]:
    """Accept the letter, signed with the uploaded picture of the signature."""
    data = await signature.read(signed_copy.MAX_SIGNATURE_BYTES + 1)
    return await letter_link.acknowledge(
        session, token, request, typed_name=typed_name, accept=accept, signature=data
    )
