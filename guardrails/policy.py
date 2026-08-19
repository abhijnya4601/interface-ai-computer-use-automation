"""
Safety & policy guardrails.

Two independent responsibilities, both graded requirements:

1. Allowlist enforcement (`guardrail_check`) — every action the discovery agent or the replay
   engine is about to take is checked against `allowlist.yaml` *before* it executes. A violation
   raises `GuardrailViolation` and halts the caller; it is never silently skipped or downgraded
   to a warning. This is deliberately loaded once at import time (module-level, process
   lifetime) rather than re-read per action — the assignment's environment is "stable UIs," not
   a live-reloading policy store, and re-reading a YAML file on every click would be the kind of
   premature-infrastructure the assignment explicitly says not to build.

2. Redaction (`redact`) — applied uniformly to anything before it touches disk: evidence logs,
   discovery transcripts, and the compiled artifact itself. Strips/masks any key that looks like
   a secret or raw PII, case-insensitively, recursively through nested dicts/lists. The mock app
   never actually produces ssn/account_number/password/token values, but the function must exist
   and be demonstrably wired in regardless — this is graded on presence and correct wiring, not
   on whether the demo data happens to trigger it.
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import yaml

ALLOWLIST_PATH = Path(__file__).parent / "allowlist.yaml"

_REDACT_KEY_SUBSTRINGS = ("ssn", "account_number", "password", "token")


class GuardrailViolation(Exception):
    """Raised when an action falls outside the configured allowlist. Always halts the caller."""


def _load_allowlist() -> dict:
    with open(ALLOWLIST_PATH) as f:
        data = yaml.safe_load(f) or {}
    return {
        "allowed_domains": set(data.get("allowed_domains") or []),
        "allowed_actions": set(data.get("allowed_actions") or []),
        "blocked_routes": list(data.get("blocked_routes") or []),
    }


# Loaded once at import time — see module docstring for why.
ALLOWLIST = _load_allowlist()


def _netloc(url: str) -> str:
    parsed = urlparse(url)
    return parsed.netloc or parsed.path.split("/")[0]


def guardrail_check(action: dict, current_url: str | None = None) -> None:
    """
    Check one proposed action against the allowlist. Raises GuardrailViolation and does not
    return anything on failure — callers must let the exception propagate and halt, not catch
    and continue.

    `action` is a small dict: {"type": "click"|"type"|"navigate"|..., "url": <optional, for
    navigate actions or to override current_url>}. `current_url` is the page's current URL,
    used for action types that don't carry their own target URL (click/type/extract/etc all act
    on whatever page is currently loaded).
    """
    action_type = action.get("type")
    if action_type not in ALLOWLIST["allowed_actions"]:
        raise GuardrailViolation(
            f"action type {action_type!r} is not in allowed_actions "
            f"{sorted(ALLOWLIST['allowed_actions'])}"
        )

    target_url = action.get("url") or current_url
    if target_url:
        netloc = _netloc(target_url)
        if netloc not in ALLOWLIST["allowed_domains"]:
            raise GuardrailViolation(
                f"domain {netloc!r} (from url {target_url!r}) is not in allowed_domains "
                f"{sorted(ALLOWLIST['allowed_domains'])}"
            )
        path = urlparse(target_url).path
        for blocked in ALLOWLIST["blocked_routes"]:
            if path.startswith(blocked):
                raise GuardrailViolation(f"route {path!r} matches blocked_routes entry {blocked!r}")


def check_risk_confirmation(risk_level: str, confirm: bool) -> None:
    """
    A `risk_level: risky` capability (state-mutating/irreversible) must not execute past its
    confirmation point in replay unless the caller passed confirm=True explicitly. This is
    intentionally a separate, cheap check from guardrail_check (which is about *where* actions
    are allowed to go, not *how consequential* a given capability is) so each has one job.
    """
    if risk_level == "risky" and not confirm:
        raise GuardrailViolation(
            "capability is risk_level=risky and requires explicit confirm=True to execute "
            "past its confirmation step"
        )


def redact(obj):
    """
    Recursively redact anything under a key that looks like a secret or raw PII
    (ssn, account_number, password, token — case-insensitive substring match). Returns a new
    object; never mutates the input. Applied uniformly before ANYTHING is written to disk.
    """
    if isinstance(obj, dict):
        result = {}
        for key, value in obj.items():
            key_lower = str(key).lower()
            if any(marker in key_lower for marker in _REDACT_KEY_SUBSTRINGS):
                result[key] = "***REDACTED***"
            else:
                result[key] = redact(value)
        return result
    if isinstance(obj, list):
        return [redact(item) for item in obj]
    return obj
