"""
Target-level runtime/exceptional-state taxonomy.

The take-home classified outcomes from a per-`capability_id` dict in `agent/compiler.py`
(`_KNOWN_OUTCOMES`) — hand-authored, doesn't generalise to a new capability. Here that becomes
a per-*target* profile: an HTTP-status map plus body-text conditions that `replay/engine.py`
applies to every step of every capability whose `target` matches, on top of any outcomes the
step itself declares.

A profile is a YAML file in this package, selected by the target host. `None` (an unknown host,
e.g. the take-home mock app) means "no profile" and replay behaves exactly as before.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

import yaml

from artifact.schema import ExpectedOutcome, TargetSpec

_DIR = Path(__file__).parent
_HOST_TO_PROFILE = {
    "web-sample.interface-hiring.com": "meridian_outcomes.yaml",
}


@lru_cache(maxsize=8)
def _load(profile_file: str) -> dict:
    return yaml.safe_load((_DIR / profile_file).read_text()) or {}


def profile_for(target: TargetSpec) -> dict | None:
    host = urlparse(target.entry_point).netloc
    name = _HOST_TO_PROFILE.get(host)
    return _load(name) if name else None


def classify(profile: dict | None, http_status: int | None, body: str) -> ExpectedOutcome | None:
    """
    Return the outcome this target's profile assigns. Body-text conditions are checked *before*
    the HTTP-status map so a specific reason wins over a generic one — MERIDIAN returns HTTP 400
    for both "source share is HOLD" and "insufficient balance", and the caller wants those
    distinguished, not both flattened to VALIDATION_REJECTED. The status map is the fallback for
    an error page whose body we don't specifically recognise (and the only signal for a bare
    interstitial like 503/440). `None` if the profile is absent or nothing matches.
    """
    if not profile:
        return None

    for cond in profile.get("body_conditions") or []:
        needle = cond.get("contains")
        if needle and needle in (body or ""):
            return ExpectedOutcome(
                condition=f"page contains '{needle}'",
                classification=cond["classification"],
                code=cond.get("code"),
                handling=cond.get("detail"),
            )

    if http_status is not None:
        table = profile.get("http_status") or {}
        entry = table.get(http_status) or table.get(str(http_status))
        if entry:
            return ExpectedOutcome(
                condition=f"HTTP {http_status}",
                classification=entry["classification"],
                code=entry.get("code"),
                handling=entry.get("detail"),
            )
    return None
