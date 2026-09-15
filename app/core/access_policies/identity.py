"""Identity, account and the home dashboard."""

ENDPOINTS: dict[str, str] = {
    "GET /auth/me": "own: the caller's own user record.",
    "GET /auth/session": "own: the caller's own user record and workspace.",
    "POST /auth/change-password": "own: changes the caller's own password.",
    "POST /auth/logout": "own: ends the caller's own session cookies.",
    "GET /dashboard/summary": "reference: tenant-wide counts; no individual's record.",
}
