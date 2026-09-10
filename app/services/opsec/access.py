"""Sessions, step-up authentication and IP allowlisting.

═══════════════════════════════════════════════════════════════════════════════
STEP-UP IS ABOUT THE ACTION, NOT ABOUT THE SESSION
═══════════════════════════════════════════════════════════════════════════════

Releasing a disbursement is the moment somebody's stolen laptop becomes a
transfer of forty lakh rupees. The session was legitimate when it started; what
matters is whether the person at the keyboard right now can prove they are the
account holder.

So `requires_step_up` is keyed on the action and `is_step_up_fresh` on a short
window since the last proof — not on session age. A twelve-hour session and a
five-minute one are equally untrustworthy at the moment of a disbursement, and
treating a fresh login as sufficient means the attacker who signed in ten
minutes ago is authorised.

`STEP_UP_ACTIONS` is a frozen set and an invariant asserts the three the brief
names are in it: releasing a disbursement, changing payroll rules, bulk export.

═══════════════════════════════════════════════════════════════════════════════
AN ALLOWLIST THAT LOCKS EVERYBODY OUT IS AN OUTAGE
═══════════════════════════════════════════════════════════════════════════════

The realistic failure of IP allowlisting is not an attacker getting in. It is a
tenant admin adding their office range, going home, and locking themselves out
of their own payroll on the 30th.

So an allowlist with no entries **allows everything** rather than nothing —
"unconfigured" and "deny all" have to be different states — and `break_glass`
is a deliberate, audited path that works from any address. A control with no
recovery is a control somebody disables entirely after the first lockout.

Pure.
"""

from __future__ import annotations

import enum
import ipaddress
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

__all__ = [
    "DEFAULT_SESSION_POLICY",
    "STEP_UP_ACTIONS",
    "STEP_UP_WINDOW",
    "AccessError",
    "AllowlistDecision",
    "SessionPolicy",
    "check_allowlist",
    "evaluate_session",
    "is_step_up_fresh",
    "requires_step_up",
]


class AccessError(ValueError):
    """An access decision this module refuses to make."""


# ══════════════════════════════════════════════════════════════════════════════
# Step-up authentication
# ══════════════════════════════════════════════════════════════════════════════

#: How recently the person must have proved who they are. Five minutes is short
#: on purpose: the point is "the person at the keyboard right now", and an hour
#: is long enough for somebody to walk away from an unlocked machine.
STEP_UP_WINDOW = timedelta(minutes=5)

#: Actions that need fresh proof of identity regardless of session age.
#:
#: The three the brief names are here and an invariant asserts each by key. The
#: others are the ones with the same shape: irreversible, or a bulk read of
#: other people's data.
STEP_UP_ACTIONS: frozenset[str] = frozenset(
    {
        # The three named in the brief.
        "disbursement.release",
        "payroll.rule_change",
        "data.bulk_export",
        # Same shape.
        "payroll.run_approve",
        "dpdp.erasure_execute",
        "retention.execute_run",
        "user.role_change",
        "tenant.sso_configure",
        "session.revoke_all",
    }
)

#: Why each one needs it, in a sentence the UI can show. A step-up prompt with
#: no explanation reads as friction and gets routed around.
_STEP_UP_REASONS: dict[str, str] = {
    "disbursement.release": (
        "This sends money. Confirm it is you before the file goes to the bank."
    ),
    "payroll.rule_change": (
        "A statutory rate applies to everybody's pay and to the filing built on "
        "it."
    ),
    "data.bulk_export": (
        "This takes a lot of people's records out of the product at once."
    ),
    "payroll.run_approve": "This finalises what everybody is paid this month.",
    "dpdp.erasure_execute": "This deletes somebody's records permanently.",
    "retention.execute_run": "This deletes or anonymises records in bulk.",
    "user.role_change": (
        "Changing what somebody can reach is how an account becomes an admin "
        "account."
    ),
    "tenant.sso_configure": (
        "Identity-provider settings decide who can sign in as anybody."
    ),
    "session.revoke_all": "This signs everybody out.",
}


def requires_step_up(action_key: str) -> bool:
    return action_key in STEP_UP_ACTIONS


def step_up_reason(action_key: str) -> str:
    """Why this action is asking. Shown at the prompt.

    An unexplained prompt is friction, and friction gets routed around — a
    shared login, a browser that remembers, an admin who does it for everybody.
    """
    if not requires_step_up(action_key):
        raise AccessError(
            f"{action_key!r} does not require step-up authentication. Prompting "
            "for it anyway trains people to confirm without reading."
        )
    return _STEP_UP_REASONS.get(
        action_key, "This action is high-impact and needs fresh confirmation."
    )


def is_step_up_fresh(
    *,
    last_verified_at: datetime | None,
    now: datetime,
    window: timedelta = STEP_UP_WINDOW,
) -> bool:
    """Whether the person has proved who they are recently enough.

    Deliberately not a function of session age. A session that began ten
    minutes ago is exactly as untrustworthy at a disbursement as one that began
    ten hours ago — an attacker who has just signed in with stolen credentials
    has a very fresh session.
    """
    if last_verified_at is None:
        return False
    if last_verified_at > now:
        raise AccessError(
            "A step-up verification is timestamped in the future. Refusing "
            "rather than treating it as fresh: a clock skew that makes a stale "
            "proof look current is the failure worth being loud about."
        )
    return now - last_verified_at <= window


# ══════════════════════════════════════════════════════════════════════════════
# Session policy
# ══════════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class SessionPolicy:
    """One tenant's session rules.

    `absolute_lifetime` is separate from `idle_timeout` because they defend
    against different things: idle timeout is the unattended laptop, and
    absolute lifetime is the stolen refresh token that would otherwise be
    renewed forever by a script.
    """

    idle_timeout_minutes: int = 60
    absolute_lifetime_hours: int = 12
    #: Zero means unlimited. A limit is a real request from clients who want
    #: one credential per person, and a bad default for everybody else — a
    #: consultant on a laptop and a phone is not an attack.
    max_concurrent_sessions: int = 0
    #: Whether an admin or payroll role is restricted to allowlisted addresses.
    enforce_ip_allowlist_for_privileged: bool = False

    def validate(self) -> None:
        if self.idle_timeout_minutes < 1:
            raise AccessError("An idle timeout of zero signs everybody out at once.")
        if self.absolute_lifetime_hours < 1:
            raise AccessError("An absolute lifetime has to be at least an hour.")
        if self.idle_timeout_minutes > self.absolute_lifetime_hours * 60:
            raise AccessError(
                "The idle timeout is longer than the absolute lifetime, so it "
                "can never fire. One of the two numbers is not what somebody "
                "meant."
            )
        if self.max_concurrent_sessions < 0:
            raise AccessError("A concurrent-session limit cannot be negative.")

    def as_dict(self) -> dict[str, Any]:
        return {
            "idle_timeout_minutes": self.idle_timeout_minutes,
            "absolute_lifetime_hours": self.absolute_lifetime_hours,
            "max_concurrent_sessions": self.max_concurrent_sessions or "unlimited",
            "enforce_ip_allowlist_for_privileged": (
                self.enforce_ip_allowlist_for_privileged
            ),
        }


DEFAULT_SESSION_POLICY = SessionPolicy()


class SessionState(str, enum.Enum):
    ACTIVE = "active"
    IDLE_EXPIRED = "idle_expired"
    LIFETIME_EXPIRED = "lifetime_expired"
    #: Explicitly ended — a forced logout, a password change, a revoke-all.
    REVOKED = "revoked"

    @property
    def is_usable(self) -> bool:
        return self is SessionState.ACTIVE


def evaluate_session(
    *,
    started_at: datetime,
    last_seen_at: datetime,
    revoked_at: datetime | None,
    tokens_valid_from: datetime | None,
    policy: SessionPolicy,
    now: datetime,
) -> SessionState:
    """Whether a session is still usable, and why not.

    `tokens_valid_from` is checked before the timeouts. A forced logout has to
    take effect immediately across every session — checking it after an idle
    calculation would leave a revoked session usable until it happened to go
    idle, which is not what "revoke" means to the person clicking it.
    """
    if revoked_at is not None and revoked_at <= now:
        return SessionState.REVOKED
    if tokens_valid_from is not None and started_at < tokens_valid_from:
        return SessionState.REVOKED
    if now - started_at >= timedelta(hours=policy.absolute_lifetime_hours):
        return SessionState.LIFETIME_EXPIRED
    if now - last_seen_at >= timedelta(minutes=policy.idle_timeout_minutes):
        return SessionState.IDLE_EXPIRED
    return SessionState.ACTIVE


def sessions_to_evict(
    *,
    active_started_at: list[datetime],
    policy: SessionPolicy,
) -> int:
    """How many of the oldest sessions a new sign-in should displace.

    Oldest first. Evicting the newest would let an attacker who has just signed
    in keep their session and push the real user out, which inverts the control.
    """
    if policy.max_concurrent_sessions <= 0:
        return 0
    excess = len(active_started_at) + 1 - policy.max_concurrent_sessions
    return max(0, excess)


# ══════════════════════════════════════════════════════════════════════════════
# IP allowlisting
# ══════════════════════════════════════════════════════════════════════════════


class AllowlistOutcome(str, enum.Enum):
    #: No entries configured. Allows everything — see the module docstring.
    NOT_CONFIGURED = "not_configured"
    ALLOWED = "allowed"
    BLOCKED = "blocked"
    #: Allowed through a deliberate, audited break-glass path.
    BREAK_GLASS = "break_glass"

    @property
    def permits(self) -> bool:
        return self is not AllowlistOutcome.BLOCKED


@dataclass(frozen=True, slots=True)
class AllowlistDecision:
    outcome: AllowlistOutcome
    matched_entry: str | None
    message: str

    @property
    def permits(self) -> bool:
        return self.outcome.permits

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "permits": self.permits,
            "matched_entry": self.matched_entry,
            "message": self.message,
        }


def check_allowlist(
    *,
    address: str,
    entries: tuple[str, ...],
    break_glass_active: bool = False,
) -> AllowlistDecision:
    """Whether this address may reach a privileged role.

    An **empty** allowlist allows everything. "Unconfigured" and "deny all" have
    to be different states: a tenant who has not set this up must not discover
    it by being locked out of their own payroll, and a control whose first
    experience is a lockout is a control they disable entirely.

    A malformed entry is refused rather than skipped. Silently ignoring
    `10.0.0.0/33` would leave an admin believing their office range is protected
    when nothing is checking it.
    """
    if not entries:
        return AllowlistDecision(
            outcome=AllowlistOutcome.NOT_CONFIGURED,
            matched_entry=None,
            message=(
                "No allowlist is configured, so every address is permitted. An "
                "empty list means unconfigured, not deny-all — a tenant must "
                "not discover this feature by being locked out of their own "
                "payroll."
            ),
        )

    try:
        candidate = ipaddress.ip_address(address)
    except ValueError as exc:
        raise AccessError(
            f"{address!r} is not an IP address. Refusing rather than blocking: "
            "a malformed address usually means a proxy header was read wrong, "
            "and silently blocking on it locks out the whole tenant."
        ) from exc

    networks: list[tuple[str, ipaddress.IPv4Network | ipaddress.IPv6Network]] = []
    for entry in entries:
        try:
            networks.append((entry, ipaddress.ip_network(entry, strict=False)))
        except ValueError as exc:
            raise AccessError(
                f"Allowlist entry {entry!r} is not a valid address or CIDR "
                "range. Skipping it would leave an admin believing their office "
                "range is protected while nothing checks it."
            ) from exc

    for entry, network in networks:
        if candidate.version == network.version and candidate in network:
            return AllowlistDecision(
                outcome=AllowlistOutcome.ALLOWED,
                matched_entry=entry,
                message=f"{address} is inside {entry}.",
            )

    if break_glass_active:
        return AllowlistDecision(
            outcome=AllowlistOutcome.BREAK_GLASS,
            matched_entry=None,
            message=(
                f"{address} is outside the allowlist and was permitted through "
                "break-glass. This is deliberate, time-limited and audited — "
                "and it is the reason a lockout does not become a support "
                "call at 2am on the 30th."
            ),
        )

    return AllowlistDecision(
        outcome=AllowlistOutcome.BLOCKED,
        matched_entry=None,
        message=(
            f"{address} is not in this tenant's allowlist for privileged roles. "
            "If this is a legitimate location, an admin can add it or use "
            "break-glass."
        ),
    )


def posture_summary() -> dict[str, Any]:
    """What a procurement questionnaire asks about sessions and access."""
    return {
        "session_defaults": DEFAULT_SESSION_POLICY.as_dict(),
        "step_up_actions": sorted(STEP_UP_ACTIONS),
        "step_up_window_minutes": int(STEP_UP_WINDOW.total_seconds() // 60),
        "step_up_statement": (
            "Step-up is keyed on the action and on how recently the person "
            "proved who they are — not on session age. A session that began ten "
            "minutes ago is exactly as untrustworthy at a disbursement as one "
            "that began ten hours ago."
        ),
        "allowlist_statement": (
            "An empty allowlist permits everything. Unconfigured and deny-all "
            "are different states, and break-glass exists so a lockout is "
            "recoverable — a control whose first experience is a lockout is one "
            "the client disables."
        ),
        "revocation_statement": (
            "A forced logout is checked before any timeout, so it takes effect "
            "on every session immediately rather than when each one happens to "
            "go idle."
        ),
        "not_built": [
            "Session rows are modelled and the policy is evaluated; the auth "
            "middleware does not yet consult either — sessions still expire "
            "only by token expiry.",
            "Step-up challenges are modelled and no second factor is "
            "implemented, so nothing can currently satisfy one.",
            "The IP allowlist is evaluated and no middleware calls it.",
        ],
    }
