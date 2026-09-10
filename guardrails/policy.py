"""
Safety & policy guardrails.

Two independent responsibilities, both graded requirements:

1. Allowlist enforcement (`guardrail_check`) - every action the discovery agent or the replay
   engine is about to take is checked against `allowlist.yaml` *before* it executes. A violation
   raises `GuardrailViolation` and halts the caller; it is never silently skipped or downgraded
   to a warning. This is deliberately loaded once at import time (module-level, process
   lifetime) rather than re-read per action - the assignment's environment is "stable UIs," not
   a live-reloading policy store, and re-reading a YAML file on every click would be the kind of
   premature-infrastructure the assignment explicitly says not to build.

2. Redaction (`redact`) - applied to anything before it touches disk (evidence logs, discovery
   transcripts, the compiled artifact) and before page content is sent to the model. Two
   independent hard passes, then a sink-aware gray-area pass:
     - by KEY (`ssn`, `account_number`, `password`, `token` - case-insensitive substring): catches
       a secret regardless of its shape, but only if the field is *named* like a secret.
     - by VALUE SHAPE (`_STRUCTURED_SECRET_PATTERNS`): catches an SSN or a card/routing number
       even sitting inside an innocuously-named field (a real observation payload, a free-text
       log line) that the key-based pass would miss. Deliberately narrow - an SSN's `###-##-####`
       shape and a 13-19-digit run are distinctive enough to flag with very low false-positive
       risk.

   Everything else - names, addresses, emails, phone numbers - is the GRAY AREA: sometimes a
   legitimate declared output, sometimes a leak, and the difference is *where the data is going*,
   not what it is. `redact(obj, sink=...)` routes that. `"artifact"` (the default) runs the two
   hard passes only and lets gray-area data through, because a capability's declared outputs are
   the point (a name in output_schema is not a leak). `"evidence"` additionally tokenizes
   gray-area entities (`<PERSON_1>`, reversible under audit via the returned map). `"llm_prompt"`
   additionally hard-masks them plus ZIP / long-digit shapes, since sending page content to a
   third-party model is a one-way door. Gray-area detection (Presidio if installed, regex
   fallback otherwise) and its per-run `RedactionReport` live in `guardrails/pii.py`;
   `redact_with_report()` returns that report for a caller that wants to log it.
"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

import yaml

ALLOWLIST_PATH = Path(__file__).parent / "allowlist.yaml"

_REDACT_KEY_SUBSTRINGS = ("ssn", "account_number", "password", "token")

_STRUCTURED_SECRET_PATTERNS = (
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),  # SSN: 123-45-6789
    re.compile(r"\b(?:\d[ -]?){13,19}\b"),  # card/routing-number-shaped digit run
)


class GuardrailViolation(Exception):
    """Raised when an action falls outside the configured allowlist. Always halts the caller."""


def _load_allowlist() -> dict:
    with open(ALLOWLIST_PATH) as f:
        data = yaml.safe_load(f) or {}
    allowed_domains = set(data.get("allowed_domains") or [])
    return {
        "allowed_domains": allowed_domains,
        # Falls back to allowed_domains if discovery_allowed_domains isn't set, for backward
        # compatibility with a bare allowlist.yaml - but a real deployment should always set
        # this explicitly and narrower than allowed_domains. See module docstring / D18.
        "discovery_allowed_domains": set(data.get("discovery_allowed_domains") or allowed_domains),
        "allowed_actions": set(data.get("allowed_actions") or []),
        "blocked_routes": list(data.get("blocked_routes") or []),
    }


# Loaded once at import time - see module docstring for why.
ALLOWLIST = _load_allowlist()


def _netloc(url: str) -> str:
    parsed = urlparse(url)
    return parsed.netloc or parsed.path.split("/")[0]


def guardrail_check(
    action: dict, current_url: str | None = None, phase: str = "replay"
) -> None:
    """
    Check one proposed action against the allowlist. Raises GuardrailViolation and does not
    return anything on failure - callers must let the exception propagate and halt, not catch
    and continue.

    `action` is a small dict: {"type": "click"|"type"|"navigate"|..., "url": <optional, for
    navigate actions or to override current_url>}. `current_url` is the page's current URL,
    used for action types that don't carry their own target URL (click/type/extract/etc all act
    on whatever page is currently loaded).

    `phase` is `"discovery"` or `"replay"` (default). Discovery is checked against the stricter
    `discovery_allowed_domains` - every discovery turn sends observed page content to a
    third-party LLM, so this is what technically enforces "discovery never touches a domain that
    isn't an approved non-production target," rather than leaving that as an unenforced
    convention. Replay never calls an LLM, so it's checked against the broader `allowed_domains`.
    """
    action_type = action.get("type")
    if action_type not in ALLOWLIST["allowed_actions"]:
        raise GuardrailViolation(
            f"action type {action_type!r} is not in allowed_actions "
            f"{sorted(ALLOWLIST['allowed_actions'])}"
        )

    domain_list_name = "discovery_allowed_domains" if phase == "discovery" else "allowed_domains"
    target_url = action.get("url") or current_url
    if target_url:
        netloc = _netloc(target_url)
        if netloc not in ALLOWLIST[domain_list_name]:
            raise GuardrailViolation(
                f"domain {netloc!r} (from url {target_url!r}) is not in {domain_list_name} "
                f"{sorted(ALLOWLIST[domain_list_name])} (phase={phase!r})"
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


def _contains_structured_secret(value: str) -> bool:
    return any(pattern.search(value) for pattern in _STRUCTURED_SECRET_PATTERNS)


def _hard_redact(obj):
    """The two always-on passes: secret-shaped KEYS and structured-secret VALUE shapes. Sink
    never changes this part - a password or an SSN is redacted everywhere, unconditionally."""
    if isinstance(obj, dict):
        result = {}
        for key, value in obj.items():
            key_lower = str(key).lower()
            if any(marker in key_lower for marker in _REDACT_KEY_SUBSTRINGS):
                result[key] = "***REDACTED***"
            else:
                result[key] = _hard_redact(value)
        return result
    if isinstance(obj, list):
        return [_hard_redact(item) for item in obj]
    if isinstance(obj, str) and _contains_structured_secret(obj):
        return "***REDACTED (structured secret pattern)***"
    return obj


def redact(obj, sink: str = "artifact"):
    """
    Redact `obj` for a given destination. Returns a new object; never mutates the input.

      - always: keys named like a secret (ssn/account_number/password/token) and values shaped
        like an SSN or card/routing number are masked.
      - sink="artifact" (default): nothing more - gray-area PII (names, addresses, emails) is
        left intact, because a capability's declared outputs are supposed to contain it. This is
        byte-for-byte the original redact() behavior.
      - sink="evidence": gray-area entities are tokenized (`<PERSON_1>`), reversibly.
      - sink="llm_prompt": gray-area entities plus ZIP / 6+ digit runs are hard-masked.

    Use `redact_with_report()` instead when you want the RedactionReport (counts, low-confidence
    review list) - e.g. to log what left for the model on each discovery turn.
    """
    return redact_with_report(obj, sink)[0]


def redact_with_report(obj, sink: str = "artifact"):
    """Like `redact`, but returns `(redacted_obj, report, token_map)`. `report` is a
    `guardrails.pii.RedactionReport` (a plain dataclass; call `.as_dict()` / `.summary_line()`).
    `token_map` is non-empty only for sink="evidence"."""
    hard = _hard_redact(obj)
    if sink == "artifact":
        from guardrails.pii import RedactionReport  # local import: keeps pii optional-dep-safe
        return hard, RedactionReport(sink=sink, backend="hard-passes-only"), {}

    from guardrails.pii import scrub_gray_area
    return scrub_gray_area(hard, sink)
