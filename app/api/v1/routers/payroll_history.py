"""Payroll history from factoHR: check, import and undo imported months.

Admin only. Thin: read the upload, delegate to
`app.services.payroll.history_import`, serialise. The preview and the issues
workbook write nothing; the import writes every month in one transaction, or
nothing while the check finds any error.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile, status

from app.core.deps import CurrentUser, DbSession, require_roles
from app.models.user import UserRole
from app.schemas.payroll_history import HistoryImportRead
from app.services.payroll import history_import

router = APIRouter(
    prefix="/payroll",
    tags=["payroll"],
    dependencies=[Depends(require_roles(UserRole.ADMIN))],
)

_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


async def _read(files: list[UploadFile]) -> list[tuple[str, bytes]]:
    return [
        (f.filename or "register.xlsx", await f.read(history_import.MAX_FILE_BYTES + 1))
        for f in files
    ]


@router.post("/history-import/preview", response_model=HistoryImportRead)
async def history_import_preview(
    files: Annotated[list[UploadFile], File()],
    user: CurrentUser,
    session: DbSession,
    legal_entity_id: Annotated[uuid.UUID | None, Form()] = None,
) -> HistoryImportRead:
    """What importing these registers would do, checked. Nothing is saved."""
    plan = await history_import.check_files(
        session, user, await _read(files), legal_entity_id=legal_entity_id
    )
    return HistoryImportRead.model_validate(history_import.preview(plan))


@router.post("/history-import/apply", response_model=HistoryImportRead)
async def history_import_apply(
    files: Annotated[list[UploadFile], File()],
    user: CurrentUser,
    session: DbSession,
    request: Request,
    legal_entity_id: Annotated[uuid.UUID | None, Form()] = None,
) -> HistoryImportRead:
    """Import the months: saved together, or not at all while any error remains."""
    plan = await history_import.check_files(
        session, user, await _read(files), legal_entity_id=legal_entity_id
    )
    plan = await history_import.apply(session, user, plan, request)
    return HistoryImportRead.model_validate(history_import.preview(plan))


@router.post("/history-import/issues")
async def history_import_issues(
    files: Annotated[list[UploadFile], File()],
    user: CurrentUser,
    session: DbSession,
    legal_entity_id: Annotated[uuid.UUID | None, Form()] = None,
) -> Response:
    """The check as a workbook: errors, warnings, notes, salary changes, months."""
    plan = await history_import.check_files(
        session, user, await _read(files), legal_entity_id=legal_entity_id
    )
    return Response(
        content=history_import.issues_xlsx(plan),
        media_type=_XLSX,
        headers={"Content-Disposition": 'attachment; filename="payroll-history-check.xlsx"'},
    )


@router.delete(
    "/runs/{run_id}/import", status_code=status.HTTP_204_NO_CONTENT, response_model=None
)
async def undo_history_import(
    run_id: uuid.UUID, user: CurrentUser, session: DbSession, request: Request
) -> None:
    """Remove one month imported from factoHR, with its payslips (salaries stay)."""
    await history_import.undo_imported_run(session, user, run_id, request)
