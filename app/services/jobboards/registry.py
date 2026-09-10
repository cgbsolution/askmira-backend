"""Adapter lookup, and the credential decryption that goes with it.

The registry exists so that nothing above this layer names a board. A caller
asks for the adapter for `JobBoard.NAUKRI` and gets a `JobBoardAdapter`; it
never imports `NaukriAdapter`, and an invariant checks that no module outside
`app/services/jobboards/` mentions a board by name.

That is a real constraint, not a stylistic one. The moment `if board == "naukri"`
appears in the hiring service, the next board added has to find every one of
those branches — and the one that gets missed is always the error path.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from app.core.crypto import decrypt_secret
from app.models.jobboard import JobBoard, JobBoardAccount
from app.services.jobboards.base import (
    BoardCredentials,
    JobBoardAdapter,
    Transport,
)
from app.services.jobboards.indeed import IndeedAdapter
from app.services.jobboards.linkedin import LinkedInAdapter
from app.services.jobboards.naukri import NaukriAdapter

__all__ = [
    "AdapterUnavailable",
    "adapter_class",
    "credentials_for",
    "get_adapter",
    "register_adapter",
    "supported_boards",
]


class AdapterUnavailable(RuntimeError):
    """No adapter for this board, or none registered in this deployment."""


#: Boards that distribute externally. CAREERS and REFERRAL are sources, not
#: destinations — there is nothing to publish to our own careers page beyond
#: making the requisition open, and a referral has no board at all.
_BUILDERS: dict[JobBoard, Any] = {
    JobBoard.NAUKRI: NaukriAdapter,
    JobBoard.LINKEDIN: LinkedInAdapter,
    JobBoard.INDEED: IndeedAdapter,
}

#: Overridable at runtime, which is how a test injects a double and how a
#: deployment can disable a board it has no contract for.
_OVERRIDES: dict[JobBoard, JobBoardAdapter] = {}


def register_adapter(board: JobBoard, adapter: JobBoardAdapter) -> None:
    _OVERRIDES[board] = adapter


def supported_boards() -> tuple[JobBoard, ...]:
    return tuple(_BUILDERS)


def adapter_class(board: JobBoard) -> type | None:
    """The adapter type for a board, without building one.

    Exists so callers can read a per-board default — a refresh interval, a rate
    limit — off the class that actually knows it, instead of branching on the
    board name somewhere else.
    """
    return _BUILDERS.get(board)


def get_adapter(board: JobBoard, transport: Transport | None = None) -> JobBoardAdapter:
    """The adapter for one board.

    Raises rather than returning `None`, because every caller of this would have
    to handle the `None` and the one that forgot would post nothing and report
    success.
    """
    if board in _OVERRIDES:
        return _OVERRIDES[board]

    builder = _BUILDERS.get(board)
    if builder is None:
        raise AdapterUnavailable(
            f"'{board.value}' is not an external job board. Applications arrive from it "
            "rather than being published to it, so there is nothing to post."
        )
    if transport is None:
        raise AdapterUnavailable(
            f"No HTTP transport is configured for {board.value}. This deployment cannot "
            "reach the board — nothing was posted, and nothing is queued."
        )
    return builder(transport)


def credentials_for(
    account: JobBoardAccount, tenant_id: uuid.UUID
) -> BoardCredentials:
    """Decrypt a board login at the point of use.

    Decrypted here and nowhere else, and never attached to the model instance —
    a credential on an ORM object ends up in a debug repr, a serialised
    response, or an error report, and this one can post jobs in the client's
    name and read their entire resume database.
    """
    if not account.credentials_encrypted:
        raise AdapterUnavailable(
            f"The {account.board.value} account has no credentials stored. Nothing was "
            "sent to the board."
        )

    secret = decrypt_secret(account.credentials_encrypted, tenant_id)
    if not secret:
        raise AdapterUnavailable(
            f"The stored {account.board.value} credential could not be decrypted. Nothing "
            "was sent — this usually means the encryption key changed since it was saved, "
            "and the credential has to be entered again."
        )

    extras: dict[str, Any] = dict(account.settings or {})
    # A board that needs more than one secret (an API key plus a client secret)
    # stores them as a JSON object; a board that needs one stores the string.
    try:
        parsed = json.loads(secret)
    except (ValueError, TypeError):
        parsed = None
    if isinstance(parsed, dict):
        extras.update({k: v for k, v in parsed.items() if k != "secret"})
        secret = str(parsed.get("secret", ""))

    return BoardCredentials(
        username=account.username or "", secret=secret, extras=extras
    )
