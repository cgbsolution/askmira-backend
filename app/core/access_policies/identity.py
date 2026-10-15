"""Identity, account and the home dashboard."""

ENDPOINTS: dict[str, str] = {
    "GET /auth/me": "own: the caller's own user record.",
    "GET /auth/session": "own: the caller's own user record and workspace.",
    "POST /auth/change-password": "own: changes the caller's own password.",
    "POST /auth/logout": "own: ends the caller's own session cookies.",
    "GET /auth/mfa": "own: whether the caller's own two-step sign-in is on.",
    "POST /auth/mfa/setup": "own: a new authenticator secret for the caller.",
    "POST /auth/mfa/enable": "own: switches on the caller's own two-step sign-in.",
    "POST /auth/mfa/disable": "own: switches off the caller's own two-step sign-in.",
    "GET /dashboard/summary": "reference: tenant-wide counts; no individual's record.",
    "GET /dashboard/quick-links": "reference: the workspace's own links; no individual's record.",
}
