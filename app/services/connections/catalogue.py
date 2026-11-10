"""Settings → Integrations: every outside system AskMiraa exchanges data with.

One row per integration family — the same list as the sales deck's integration
catalogue — each stating, plainly:

- **status**: `available` (works today with nothing to set up beyond using it)
  or `configurable` (works once this workspace connects its own account, or the
  deployment has the provider's app registered);
- **direction**, **frequency**, **auth** and **limitations**, in words a
  customer's IT team can check;
- **this workspace's state**, read from its own records: `connected`, `ready`
  (nothing stands in the way; just use it), `needs_setup` (the workspace has to
  connect something), or `platform_setup` (the deployment has to register the
  provider's app first, which no workspace admin can do).

Counts only — no names, no secrets — so any admin may read it.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import tenant_scoped
from app.models.accounting import LedgerMapping
from app.models.bi import BiConnection
from app.models.disbursement import BankProfile
from app.models.filings import FilingRecord
from app.models.identity import DeviceIntegration, IdentityProvider, ScimToken
from app.models.integrations import ApiKey, CalendarFeedToken, WebhookEndpoint
from app.models.interview import CalendarConnection, CalendarProviderKind
from app.models.jobboard import JobBoardAccount
from app.models.notifications import TenantChatApp, TenantWhatsAppSettings
from app.models.oauth import OAuthGrant
from app.models.tenant import Tenant
from app.models.user import User
from app.services.connections import store

__all__ = ["catalogue"]


async def _count(session: AsyncSession, user: User, model, *conditions) -> int:
    stmt = select(func.count()).select_from(model)
    for condition in conditions:
        stmt = stmt.where(condition)
    return int(await session.scalar(tenant_scoped(stmt, model, user)) or 0)


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


async def catalogue(session: AsyncSession, user: User) -> list[dict[str, Any]]:
    from app.services.identity import social
    from app.services.interviews.calendar_oauth import oauth_client
    from app.services.products.catalogue import PRODUCTS

    tenant = await session.get(Tenant, user.tenant_id)
    modules = (tenant.enabled_modules or {}) if tenant is not None else {}
    # A module our separate product runs for this workspace (hiring in AnytimeHire).
    replaced_by = {
        PRODUCTS[key].replaces: PRODUCTS[key].brand
        for key in (tenant.linked_products if tenant is not None else {})
        if key in PRODUCTS and PRODUCTS[key].replaces
    }
    hiring_by = replaced_by.get("hiring")

    rows: list[dict[str, Any]] = []

    # ── Banks ────────────────────────────────────────────────────────────────
    banks = (
        await session.scalars(
            tenant_scoped(
                select(BankProfile.bank_name).where(BankProfile.active.is_(True)), BankProfile, user
            )
        )
    ).all()
    rows.append(
        {
            "key": "banks",
            "integration": "HDFC, ICICI, SBI, Axis, Kotak",
            "category": "Banks",
            "data": "Payroll salary files in the bank's format",
            "status": "available",
            "direction": "AskMiraa → bank (a file HR uploads to the bank's corporate portal)",
            "frequency": "Each payroll run, on demand",
            "auth": "None in AskMiraa — the upload happens in the bank's own portal",
            "limitations": (
                "A file, not a direct bank API. Other banks use the generic NEFT format. "
                "The bank's response file is read back to mark each payment paid or failed."
            ),
            "state": "connected" if banks else "needs_setup",
            "detail": (
                f"{_plural(len(banks), 'bank profile')}: {', '.join(sorted(set(banks)))}"
                if banks
                else "Add the company's bank account and its file format."
            ),
            "href": "/payroll/disbursements",
        }
    )

    # ── Accounting ──────────────────────────────────────────────────────────
    mapped = await _count(session, user, LedgerMapping)
    books = await store.get(session, user, store.KIND_ZOHO_BOOKS)
    books_on = bool(books is not None and books.enabled and books.secret_encrypted)
    rows.append(
        {
            "key": "accounting",
            "integration": "Tally, Zoho Books",
            "category": "Accounting",
            "data": "Payroll journals posted to your books",
            "status": "available",
            "direction": "AskMiraa → books",
            "frequency": "Each approved payroll run",
            "auth": (
                "Tally: none (XML file imported in Tally). Zoho Books: your Zoho login (OAuth), "
                "connected here"
            ),
            "limitations": (
                "Every payslip line must be mapped to a ledger first; an unmapped line stops the "
                "journal rather than guessing. Tally takes the journal as an XML import — Tally "
                "runs on your own computer, which a cloud service cannot reach. Zoho Books is "
                "posted directly once connected."
            ),
            "state": "connected" if mapped and books_on else ("ready" if mapped else "needs_setup"),
            "detail": " · ".join(
                [
                    _plural(mapped, "ledger mapping") if mapped else "No ledgers mapped yet",
                    (
                        f"Zoho Books connected ({books.account_label})"
                        if books_on and books is not None
                        else "Zoho Books not connected"
                    ),
                ]
            ),
            "href": "/payroll/accounting",
        }
    )

    # ── Statutory ───────────────────────────────────────────────────────────
    filings = await _count(session, user, FilingRecord)
    rows.append(
        {
            "key": "statutory",
            "integration": "EPFO ECR, ESI, PT, LWF, 24Q, Form 16",
            "category": "Statutory",
            "data": "Statutory files generated from the payroll run",
            "status": "available",
            "direction": "AskMiraa → government portals (files HR uploads)",
            "frequency": "Monthly (ECR, ESI, PT), half-yearly or yearly (LWF), quarterly (24Q), "
            "yearly (Form 16)",
            "auth": "None in AskMiraa — uploaded on EPFO, ESIC, the state PT site and TRACES",
            "limitations": (
                "Files, not a direct filing API: the government portals take uploads only. "
                "LWF comes as the state register with employee and employer shares. Form 16 "
                "Part A is TRACES' own file, imported."
            ),
            "state": "connected" if filings else "ready",
            "detail": (
                f"{_plural(filings, 'return')} generated"
                if filings
                else "Generated from an approved payroll run"
            ),
            "href": "/payroll/filings",
        }
    )

    # ── Attendance devices ──────────────────────────────────────────────────
    devices = await _count(session, user, DeviceIntegration)
    rows.append(
        {
            "key": "devices",
            "integration": "eSSL, ZKTeco, kiosk",
            "category": "Attendance devices",
            "data": "Biometric and kiosk punches",
            "status": "configurable",
            "direction": "Device → AskMiraa",
            "frequency": "Live, as the device pushes (ADMS); kiosk punches at once",
            "auth": "A per-device token, or the device's serial number when switched on for it",
            "limitations": (
                "The device must reach AskMiraa over the internet (ADMS push). No commands or "
                "user lists are sent to a device. A silent device is flagged."
            ),
            "state": "connected" if devices else "needs_setup",
            "detail": _plural(devices, "device") if devices else "Register a device",
            "href": "/settings/devices",
        }
    )

    # ── Identity ────────────────────────────────────────────────────────────
    idps = await _count(session, user, IdentityProvider, IdentityProvider.enabled.is_(True))
    scim = await _count(
        session, user, ScimToken, ScimToken.active.is_(True), ScimToken.revoked_at.is_(None)
    )
    social_ready = social.available()
    rows.append(
        {
            "key": "identity",
            "integration": "Google, Microsoft, OIDC SSO; SCIM",
            "category": "Identity",
            "data": "Sign-in and user provisioning",
            "status": "configurable",
            "direction": "Identity provider → AskMiraa",
            "frequency": "At each sign-in; SCIM as your directory changes",
            "auth": "OIDC or SAML with your identity provider; a SCIM bearer token",
            "limitations": (
                "An identity provider never grants a role: people arrive as employees and HR "
                "sets roles. A break-glass admin keeps password sign-in when SSO is enforced."
            ),
            "state": "connected" if (idps or scim) else "needs_setup",
            "detail": " · ".join(
                [
                    _plural(idps, "identity provider") if idps else "No SSO provider",
                    _plural(scim, "SCIM token") if scim else "no SCIM token",
                    (
                        "Continue with " + " / ".join(k.title() for k in social_ready) + " on"
                        if social_ready
                        else "Google / Microsoft buttons need the platform's app"
                    ),
                ]
            ),
            "href": "/settings/sso",
        }
    )

    # ── Job boards ──────────────────────────────────────────────────────────
    boards = (
        await session.scalars(
            tenant_scoped(
                select(JobBoardAccount.board).where(JobBoardAccount.is_active.is_(True)),
                JobBoardAccount,
                user,
            )
        )
    ).all()
    rows.append(
        {
            "key": "job_boards",
            "integration": "LinkedIn, Naukri, Indeed",
            "category": "Job boards",
            "data": "Job posting from one place",
            "status": "configurable",
            "direction": "AskMiraa → board; applicants back through the careers page or webhook",
            "frequency": "On posting; Naukri refreshed weekly; Indeed reads the feed daily",
            "auth": (
                "LinkedIn: your partner API token. Naukri: the API key and address from your "
                "Naukri API agreement. Indeed: none (a public job feed)"
            ),
            "limitations": (
                "LinkedIn's job API is open to approved partners only, and Naukri's to "
                "customers with an API agreement — without them the board refuses and the "
                "posting shows why. Indeed lists jobs from the feed on its own schedule."
            ),
            "state": "ready" if hiring_by else ("connected" if boards else "needs_setup"),
            "detail": (
                f"Hiring runs in {hiring_by} for this workspace: jobs are posted to boards there"
                if hiring_by
                else (
                    ", ".join(sorted(b.value.title() for b in boards)) + " added"
                    if boards
                    else "Add a board account"
                )
            ),
            "href": "/hiring/job-boards",
        }
    )

    # ── Calendars ───────────────────────────────────────────────────────────
    calendars_ready = [p for p in CalendarProviderKind if oauth_client(p) is not None]
    connected_people = await _count(
        session, user, CalendarConnection, CalendarConnection.revoked_at.is_(None)
    )
    feeds = await _count(session, user, CalendarFeedToken, CalendarFeedToken.revoked_at.is_(None))
    rows.append(
        {
            "key": "calendars",
            "integration": "Google Calendar, Microsoft 365",
            "category": "Calendars",
            "data": "Calendar events",
            "status": "available",
            "direction": (
                "Interviews: both ways (free/busy read, invitations written). Holidays and "
                "leave: AskMiraa → calendar by subscription"
            ),
            "frequency": (
                "Interviews live, as they are planned, moved or cancelled; the holiday and "
                "leave link is re-read by the calendar app every few hours"
            ),
            "auth": (
                "Interviews: each interviewer's own Google or Microsoft sign-in (OAuth), "
                "renewed automatically. Holidays and leave: a private link per person"
            ),
            "limitations": (
                "Interview scheduling needs the platform's Google / Microsoft calendar app. The "
                "holiday and leave link works in any calendar app and is read-only; a "
                "calendar that cannot be read is shown as unknown, never as free."
            ),
            "state": "connected" if (connected_people or feeds) else "ready",
            "detail": " · ".join(
                [
                    (
                        f"Interviews are scheduled in {hiring_by}"
                        if hiring_by
                        else f"{_plural(connected_people, 'interviewer')} connected"
                        if connected_people
                        else (
                            "Interviewers connect their own calendar on Interviews"
                            if calendars_ready
                            else "Interview scheduling needs the platform's calendar app"
                        )
                    ),
                    f"{_plural(feeds, 'person', 'people')} subscribed to holidays and leave"
                    if feeds
                    else "Holiday and leave links: Leave → Holidays → Add to my calendar",
                ]
            ),
            "href": "/hiring/interviews",
        }
    )

    # ── Messaging ───────────────────────────────────────────────────────────
    whatsapp = await session.scalar(
        tenant_scoped(select(TenantWhatsAppSettings), TenantWhatsAppSettings, user)
    )
    whatsapp_on = bool(whatsapp is not None and whatsapp.enabled)
    rows.append(
        {
            "key": "messaging",
            "integration": "WhatsApp Business, branded email",
            "category": "Messaging",
            "data": "Messages and notifications",
            "status": "available",
            "direction": "AskMiraa → people",
            "frequency": "As events happen; daily digests where people choose them",
            "auth": "Email: built in, or your own SMTP. WhatsApp: your Meta business number "
            "and token",
            "limitations": (
                "WhatsApp only reaches people who opted in, with Meta-approved templates. "
                "Email always works."
            ),
            "state": "connected" if whatsapp_on else "ready",
            "detail": (
                "Branded email on · WhatsApp connected"
                if whatsapp_on
                else "Branded email on · WhatsApp not connected"
            ),
            "href": "/settings/notifications",
        }
    )

    # ── Approvals in chat ───────────────────────────────────────────────────
    chat = (
        await session.scalars(
            tenant_scoped(
                select(TenantChatApp.provider).where(TenantChatApp.enabled.is_(True)),
                TenantChatApp,
                user,
            )
        )
    ).all()
    rows.append(
        {
            "key": "chat",
            "integration": "Slack, Microsoft Teams",
            "category": "Approvals in chat",
            "data": "Approve and decide from chat",
            "status": "configurable",
            "direction": "Both ways: requests sent to chat, Approve / Reject come back",
            "frequency": "As requests arrive",
            "auth": "Your own Slack app or Teams bot, created from the manifest given here",
            "limitations": (
                "Direct messages to people whose chat account matches their work email."
            ),
            "state": "connected" if chat else "needs_setup",
            "detail": (
                ", ".join(sorted(c.value.title() for c in chat)) + " connected"
                if chat
                else "Connect Slack or Teams"
            ),
            "href": "/settings/notifications",
        }
    )

    # ── eSign ───────────────────────────────────────────────────────────────
    digio = await store.get(session, user, store.KIND_DIGIO)
    digio_on = bool(digio is not None and digio.enabled and digio.secret_encrypted)
    deployment_digio = (settings.SIGNATURE_PROVIDER or "").lower() == "digio" and bool(
        settings.DIGIO_CLIENT_ID and settings.DIGIO_CLIENT_SECRET
    )
    rows.append(
        {
            "key": "esign",
            "integration": "Digio",
            "category": "eSign",
            "data": "Aadhaar eSign on letters",
            "status": "configurable",
            "direction": "AskMiraa → Digio (letter); Digio → AskMiraa (signed copy)",
            "frequency": "Per letter; status by webhook, or checked hourly",
            "auth": "Your Digio client id and secret (sandbox or production)",
            "limitations": (
                "Digio bills each signature to your Digio account. Digio emails the signer the "
                "link. Offer letters are signed from the offer, not here."
            ),
            "state": "connected" if (digio_on or deployment_digio) else "needs_setup",
            "detail": (
                f"Your Digio account ({(digio.settings or {}).get('environment', 'production')})"
                if digio_on and digio is not None
                else (
                    "Using the platform's Digio account"
                    if deployment_digio
                    else "Connect your Digio account below"
                )
            ),
            "href": "/settings/integrations#digio",
        }
    )

    # ── External AI (MCP) ───────────────────────────────────────────────────
    grants = await _count(session, user, OAuthGrant, OAuthGrant.revoked_at.is_(None))
    keys = await _count(session, user, ApiKey, ApiKey.revoked_at.is_(None))
    rows.append(
        {
            "key": "mcp",
            "integration": "Claude, ChatGPT (MCP)",
            "category": "External AI",
            "data": "Ask Miraa tools over MCP",
            "status": "available",
            "direction": "AI assistant → AskMiraa (read)",
            "frequency": "When the assistant asks",
            "auth": "OAuth sign-in from the assistant, or a read-only API key",
            "limitations": (
                "Read-only tools; never pay, bank, tax, health or leave reasons. Every call is "
                "in the audit trail."
            ),
            "state": "connected" if grants else "ready",
            "detail": (
                f"{_plural(grants, 'assistant')} signed in"
                if grants
                else "Add AskMiraa as a connector in Claude or ChatGPT"
            ),
            "href": "/settings?tab=integrations",
        }
    )

    # ── BI ──────────────────────────────────────────────────────────────────
    bi = await _count(session, user, BiConnection, BiConnection.is_active.is_(True))
    rows.append(
        {
            "key": "bi",
            "integration": "Power BI, Excel, own bucket",
            "category": "BI",
            "data": "Nightly files or a feed",
            "status": "available",
            "direction": "AskMiraa → your BI tool or bucket",
            "frequency": "Nightly at 02:00 India time, or whenever the BI tool refreshes",
            "auth": "A private feed link, or your bucket's access key (S3-compatible)",
            "limitations": (
                "Fields and rows follow the admin who set the connection up. No direct database "
                "access."
            ),
            "state": "connected" if bi else "ready",
            "detail": _plural(bi, "connection") if bi else "Create a feed or bucket delivery",
            "href": "/reports/bi",
        }
    )

    # ── Open integration ────────────────────────────────────────────────────
    hooks = await _count(session, user, WebhookEndpoint, WebhookEndpoint.active.is_(True))
    rows.append(
        {
            "key": "api",
            "integration": "REST API, signed webhooks",
            "category": "Open integration",
            "data": "Build-your-own connections",
            "status": "available",
            "direction": "Both ways: read and write by API; events out by webhook",
            "frequency": "On request; webhooks within a minute of the change",
            "auth": "API keys with scopes; webhooks signed with HMAC-SHA256",
            "limitations": "Webhooks go to https addresses on the public internet only.",
            "state": "connected" if (keys or hooks) else "ready",
            "detail": " · ".join(
                [
                    _plural(keys, "API key") if keys else "No API keys",
                    _plural(hooks, "webhook") if hooks else "no webhooks",
                ]
            ),
            "href": "/settings?tab=integrations",
        }
    )
    # A row whose module is switched off cannot be used here, whatever is stored.
    module_of = {
        "banks": "payroll",
        "accounting": "payroll",
        "statutory": "payroll",
        "devices": "leave",
        "job_boards": "hiring",
        "esign": "core_hr",
        "bi": "analytics",
    }
    for row in rows:
        module = module_of.get(row["key"])
        if module and module not in replaced_by and not modules.get(module):
            row["state"] = "needs_setup"
            row["detail"] = (
                f"The {module.replace('_', ' ').title()} module is off for this workspace "
                "(Settings → Modules)"
            )
            row["href"] = "/settings/modules"
    return rows
