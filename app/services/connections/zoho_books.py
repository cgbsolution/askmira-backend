"""Zoho Books: an approved payroll run's journal, posted straight into the books.

**Connecting.** Zoho lets a company give one server access to its own books
through a *Self Client* in the Zoho API Console: the admin creates it, generates
a one-time code for the Books journal scopes, and pastes the client id, secret
and code here. AskMira swaps the code for a refresh token at Zoho at once (the
code lasts minutes), reads which Books organisations the login can reach, and
keeps the refresh token encrypted with the tenant key. No app has to be
registered by the platform, and nothing is kept if Zoho refuses.

**Posting.** The journal is the one the Tally and Zoho file exports produce —
the same balanced lines, the same refusals (an imported month, an unmapped
line, a journal that does not balance). Each ledger name is matched to an
account in the organisation's chart of accounts, by name; a ledger Zoho does
not have stops the post with the list, rather than creating a stray account.
A run is posted at most once per organisation: a second post is refused with
the journal number it already has.

Shapes follow Zoho Books API v3 and are exercised against a double here; run
one connect and one post on a real organisation before relying on it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import tenant_scoped
from app.models.accounting import PayrollJournalPosting
from app.models.audit import AuditAction
from app.models.integrations import TenantConnection
from app.models.payroll import PayrollRun
from app.models.user import User
from app.services import audit
from app.services.connections import store
from app.services.payroll import journal as journal_service

__all__ = [
    "DATA_CENTRES",
    "SCOPES",
    "choose_organisation",
    "connect",
    "disconnect",
    "post_run",
    "posting_for_run",
    "run_posting_status",
    "status_for",
]

LABEL = "Zoho Books"
TARGET = store.KIND_ZOHO_BOOKS
#: The scopes the one-time code must be generated with.
SCOPES = "ZohoBooks.accountants.CREATE,ZohoBooks.accountants.READ,ZohoBooks.settings.READ"
#: Zoho keeps each customer's data in one data centre: accounts server, API server.
DATA_CENTRES: dict[str, tuple[str, str, str]] = {
    "in": ("India (zoho.in)", "https://accounts.zoho.in", "https://www.zohoapis.in"),
    "com": ("United States (zoho.com)", "https://accounts.zoho.com", "https://www.zohoapis.com"),
    "eu": ("Europe (zoho.eu)", "https://accounts.zoho.eu", "https://www.zohoapis.eu"),
    "com.au": (
        "Australia (zoho.com.au)",
        "https://accounts.zoho.com.au",
        "https://www.zohoapis.com.au",
    ),
    "jp": ("Japan (zoho.jp)", "https://accounts.zoho.jp", "https://www.zohoapis.jp"),
    "ca": ("Canada (zohocloud.ca)", "https://accounts.zohocloud.ca", "https://www.zohoapis.ca"),
    "sa": ("Saudi Arabia (zoho.sa)", "https://accounts.zoho.sa", "https://www.zohoapis.sa"),
}
TIMEOUT = 20.0


def _bad(detail: str, code: int = status.HTTP_422_UNPROCESSABLE_ENTITY) -> HTTPException:
    return HTTPException(status_code=code, detail=detail)


def _zoho_message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    if isinstance(body, dict):
        return (
            str(body.get("message") or body.get("error_description") or body.get("error") or "")
            or f"HTTP {response.status_code}"
        )
    return f"HTTP {response.status_code}"


async def _token(
    accounts: str, form: dict[str, str], *, transport: httpx.AsyncBaseTransport | None
) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, transport=transport) as http:
            response = await http.post(f"{accounts}/oauth/v2/token", data=form)
    except httpx.HTTPError as exc:
        raise _bad(
            f"Could not reach Zoho ({type(exc).__name__}). Try again shortly.",
            status.HTTP_502_BAD_GATEWAY,
        ) from exc
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code >= 400 or not isinstance(body, dict) or "access_token" not in body:
        reason = body.get("error") if isinstance(body, dict) else None
        if reason == "invalid_code":
            raise _bad(
                "Zoho says the code is invalid or has expired (a code lasts only a few "
                "minutes and works once). Generate a new code and paste it straight away."
            )
        if reason == "invalid_client":
            raise _bad(
                "Zoho does not recognise this client id and secret in the data centre chosen."
            )
        raise _bad(f"Zoho refused the sign-in{f' ({reason})' if reason else ''}.")
    return body


async def _get(
    api: str,
    path: str,
    token: str,
    *,
    params: dict[str, Any] | None = None,
    transport: httpx.AsyncBaseTransport | None,
) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, transport=transport) as http:
            response = await http.get(
                f"{api}/books/v3{path}",
                params=params,
                headers={"Authorization": f"Zoho-oauthtoken {token}"},
            )
    except httpx.HTTPError as exc:
        raise _bad(
            f"Could not reach Zoho Books ({type(exc).__name__}).", status.HTTP_502_BAD_GATEWAY
        ) from exc
    if response.status_code >= 400:
        raise _bad(f"Zoho Books refused: {_zoho_message(response)}", status.HTTP_502_BAD_GATEWAY)
    body = response.json()
    return body if isinstance(body, dict) else {}


def _view(row: TenantConnection | None) -> dict[str, Any]:
    out = store.read(row)
    settings = (row.settings or {}) if row is not None else {}
    out["organisations"] = settings.get("organisations", [])
    out["organisation_id"] = settings.get("organization_id")
    out["needs_organisation"] = bool(row is not None and not settings.get("organization_id"))
    out["data_centres"] = [{"key": k, "label": v[0]} for k, v in DATA_CENTRES.items()]
    out["scopes"] = SCOPES
    return out


async def status_for(session: AsyncSession, user: User) -> dict[str, Any]:
    return _view(await store.get(session, user, TARGET))


async def connect(
    session: AsyncSession,
    user: User,
    *,
    data_centre: str,
    client_id: str,
    client_secret: str,
    code: str,
    request: Request | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Swap the one-time code for a refresh token and keep it. Nothing is kept on a refusal."""
    centre = DATA_CENTRES.get((data_centre or "").strip().lower())
    if centre is None:
        raise _bad("Choose the Zoho data centre your Books organisation is in.")
    client_id, client_secret, code = (
        (client_id or "").strip(),
        (client_secret or "").strip(),
        (code or "").strip(),
    )
    if not client_id or not client_secret or not code:
        raise _bad("Enter the client id, the client secret and the code from the Zoho API Console.")
    _, accounts, api = centre

    tokens = await _token(
        accounts,
        {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
        },
        transport=transport,
    )
    refresh = tokens.get("refresh_token")
    if not refresh:
        raise _bad(
            "Zoho gave no refresh token, so AskMira could post only for the next hour. "
            "Generate the code again (a Self Client code gives one the first time it is used)."
        )
    api = str(tokens.get("api_domain") or api).rstrip("/")
    organisations = (
        await _get(api, "/organizations", str(tokens["access_token"]), transport=transport)
    ).get("organizations") or []
    orgs = [
        {"id": str(o.get("organization_id")), "name": str(o.get("name") or "")}
        for o in organisations
        if o.get("organization_id")
    ]
    if not orgs:
        raise _bad("This Zoho login reaches no Zoho Books organisation.")
    chosen = orgs[0] if len(orgs) == 1 else None

    row = await store.save(
        session,
        user,
        TARGET,
        settings={
            "data_centre": data_centre.strip().lower(),
            "client_id": client_id,
            "api_domain": api,
            "organisations": orgs,
            "organization_id": chosen["id"] if chosen else None,
            "organization_name": chosen["name"] if chosen else None,
        },
        secrets={"client_secret": client_secret, "refresh_token": str(refresh)},
        account_label=chosen["name"] if chosen else None,
        label=LABEL,
        request=request,
    )
    return _view(row)


async def choose_organisation(
    session: AsyncSession, user: User, organisation_id: str, *, request: Request | None = None
) -> dict[str, Any]:
    row = await store.get(session, user, TARGET)
    if row is None:
        raise _bad("Zoho Books is not connected.", status.HTTP_404_NOT_FOUND)
    settings = dict(row.settings or {})
    match = next(
        (o for o in settings.get("organisations", []) if o.get("id") == organisation_id), None
    )
    if match is None:
        raise _bad("That organisation is not one this Zoho login can reach.")
    settings["organization_id"] = match["id"]
    settings["organization_name"] = match["name"]
    row = await store.save(
        session,
        user,
        TARGET,
        settings=settings,
        secrets=None,
        account_label=match["name"],
        label=LABEL,
        request=request,
    )
    return _view(row)


async def disconnect(session: AsyncSession, user: User, *, request: Request | None = None) -> None:
    if not await store.remove(session, user, TARGET, label=LABEL, request=request):
        raise _bad("Zoho Books is not connected.", status.HTTP_404_NOT_FOUND)


async def posting_for_run(
    session: AsyncSession, user: User, run_id: uuid.UUID
) -> PayrollJournalPosting | None:
    return await session.scalar(
        tenant_scoped(
            select(PayrollJournalPosting).where(
                PayrollJournalPosting.run_id == run_id, PayrollJournalPosting.target == TARGET
            ),
            PayrollJournalPosting,
            user,
        )
    )


def _posting_read(row: PayrollJournalPosting) -> dict[str, Any]:
    return {
        "posted": True,
        "journal_id": row.external_id,
        "journal_number": row.external_number,
        "amount": str(row.amount),
        "posted_at": row.posted_at.isoformat(),
    }


async def _accounts(
    api: str, token: str, organisation_id: str, transport: httpx.AsyncBaseTransport | None
) -> dict[str, str]:
    """Account name (case-folded) → account id, across every page of the chart."""
    found: dict[str, str] = {}
    page = 1
    while page <= 20:
        body = await _get(
            api,
            "/chartofaccounts",
            token,
            params={"organization_id": organisation_id, "page": page, "per_page": 200},
            transport=transport,
        )
        for account in body.get("chartofaccounts") or []:
            if account.get("is_active") is False:
                continue
            name = str(account.get("account_name") or "").strip().casefold()
            if name and account.get("account_id"):
                found.setdefault(name, str(account["account_id"]))
        if not (body.get("page_context") or {}).get("has_more_page"):
            break
        page += 1
    return found


async def post_run(
    session: AsyncSession,
    user: User,
    run_id: uuid.UUID,
    *,
    by_department: bool = False,
    request: Request | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Post an approved run's journal to the connected Zoho Books organisation, once."""
    row = await store.get(session, user, TARGET)
    if row is None or not row.enabled or not row.secret_encrypted:
        raise _bad(
            "Connect Zoho Books under Settings → Integrations first.", status.HTTP_409_CONFLICT
        )
    settings = row.settings or {}
    organisation_id = settings.get("organization_id")
    if not organisation_id:
        raise _bad(
            "Choose which Zoho Books organisation to post to under Settings → Integrations.",
            status.HTTP_409_CONFLICT,
        )
    # Hold the run while posting: a second click waits here until the first has
    # committed its posting, then finds it below instead of posting again.
    await session.execute(
        tenant_scoped(
            select(PayrollRun.id).where(PayrollRun.id == run_id).with_for_update(),
            PayrollRun,
            user,
        )
    )
    existing = await posting_for_run(session, user, run_id)
    if existing is not None:
        raise _bad(
            f"This run is already in Zoho Books as journal "
            f"{existing.external_number or existing.external_id} (posted "
            f"{existing.posted_at:%d %b %Y}). Posting it again would book the month's salary "
            "twice.",
            status.HTTP_409_CONFLICT,
        )

    run, lines, meta = await journal_service.prepare_journal(
        session, user, run_id, by_department=by_department
    )

    centre = DATA_CENTRES.get(str(settings.get("data_centre") or "in"), DATA_CENTRES["in"])
    secrets = store.secrets_of(row)
    tokens = await _token(
        centre[1],
        {
            "grant_type": "refresh_token",
            "client_id": str(settings.get("client_id") or ""),
            "client_secret": str(secrets.get("client_secret") or ""),
            "refresh_token": str(secrets.get("refresh_token") or ""),
        },
        transport=transport,
    )
    token = str(tokens["access_token"])
    api = str(settings.get("api_domain") or centre[2])

    accounts = await _accounts(api, token, str(organisation_id), transport)
    missing = sorted(
        {line.ledger for line in lines if line.ledger.strip().casefold() not in accounts}
    )
    if missing:
        raise _bad(
            f"Zoho Books has no account named {', '.join(missing)}. Create them in Zoho Books, "
            "or change the ledger names under Payroll → Accounting to match Zoho's, then post "
            "again. Nothing was posted.",
            status.HTTP_409_CONFLICT,
        )

    line_items = []
    for line in lines:
        side = "debit" if line.debit else "credit"
        amount: Decimal = line.debit or line.credit
        line_items.append(
            {
                "account_id": accounts[line.ledger.strip().casefold()],
                "debit_or_credit": side,
                # Two decimals, already quantised; a float of that is exact enough for
                # Zoho's JSON, and the total is checked from the Decimals, not this.
                "amount": float(amount),
                "description": line.cost_centre or meta.narration[:100],
            }
        )
    total = sum((line.debit for line in lines), start=Decimal(0))
    payload = {
        "journal_date": meta.voucher_date.isoformat(),
        "reference_number": meta.voucher_number,
        "notes": meta.narration,
        "line_items": line_items,
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, transport=transport) as http:
            response = await http.post(
                f"{api}/books/v3/journals",
                params={"organization_id": organisation_id},
                json=payload,
                headers={"Authorization": f"Zoho-oauthtoken {token}"},
            )
    except httpx.HTTPError as exc:
        raise _bad(
            f"Could not reach Zoho Books ({type(exc).__name__}). Nothing was posted; try again.",
            status.HTTP_502_BAD_GATEWAY,
        ) from exc
    body: dict[str, Any] = {}
    try:
        parsed = response.json()
        body = parsed if isinstance(parsed, dict) else {}
    except ValueError:
        body = {}
    journal = body.get("journal") if isinstance(body.get("journal"), dict) else None
    if response.status_code >= 400 or body.get("code") not in (0, None) or journal is None:
        message = _zoho_message(response)
        row.last_error = message
        await session.commit()
        raise _bad(f"Zoho Books did not take the journal: {message}", status.HTTP_502_BAD_GATEWAY)

    posting = PayrollJournalPosting(
        tenant_id=user.tenant_id,
        run_id=run.id,
        target=TARGET,
        external_id=str(journal.get("journal_id") or ""),
        external_number=str(journal.get("entry_number") or "") or None,
        amount=total,
        posted_at=datetime.now(UTC),
        posted_by_user_id=user.id,
    )
    session.add(posting)
    row.last_error = None
    await session.flush()
    await audit.record(
        session,
        tenant_id=user.tenant_id,
        action=AuditAction.EXPORT,
        entity_type="payroll_run",
        entity_id=run.id,
        actor=user,
        changes={
            "posted_to": LABEL,
            "organisation": settings.get("organization_name"),
            "journal_id": posting.external_id,
            "lines": len(lines),
            "amount": str(total),
        },
        summary=(
            f"Posted the payroll journal for {run.period_month:02d}/{run.period_year} to {LABEL}"
        ),
        request=request,
    )
    await session.commit()
    return _posting_read(posting)


async def run_posting_status(session: AsyncSession, user: User, run_id: uuid.UUID) -> dict:
    """Whether this run is in Zoho Books, and whether it can be posted there."""
    row = await store.get(session, user, TARGET)
    posting = await posting_for_run(session, user, run_id)
    if posting is not None:
        return {"connected": row is not None, **_posting_read(posting)}
    return {
        "connected": bool(row is not None and row.enabled and row.secret_encrypted),
        "organisation": (row.settings or {}).get("organization_name") if row else None,
        "posted": False,
    }
