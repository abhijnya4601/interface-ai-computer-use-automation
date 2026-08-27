"""
Session bootstrap for targets that gate every function behind an operator sign-on.

MERIDIAN CORE issues an ``MC_SID`` cookie at ``/signon`` and 302s every other route to the login
page without it; sessions also time out on idle. The take-home core is one-browser-one-capability
with no session concept. This module adds the missing piece without changing that contract:

  - ``meridian_signon`` is a normal recorded capability (it IS one of the target's §2.1
    functions) with ``operator`` / ``password`` / ``branch`` as typed params — no credential is
    stored in the artifact (see scripts/record_meridian_signon.py),
  - ``run_with_session`` opens one browser, replays ``meridian_signon`` against it with
    credentials pulled from the environment, then replays the *target* capability on that same
    authenticated page. ``replay()`` already accepts a caller-owned ``page``; sessions stay
    entirely outside it.

Credentials come from the environment only — never CLI args, never a request body, never an
artifact — the same trust model as ``ANTHROPIC_API_KEY`` and ``OPERATOR_PASSWORD``.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from artifact.schema import Capability, Result
from replay.engine import replay

CAPABILITIES_DIR = Path(__file__).parent.parent / "capabilities"
SIGNON_CAPABILITY_ID = "meridian_signon"
DEFAULT_BRANCH = "MAIN-001"

# role -> (operator env var, password env var, branch env var)
_CREDENTIAL_ENV = {
    "teller": ("MERIDIAN_OPERATOR", "MERIDIAN_PASSWORD", "MERIDIAN_BRANCH"),
    "supervisor": (
        "MERIDIAN_SUPERVISOR_OPERATOR",
        "MERIDIAN_SUPERVISOR_PASSWORD",
        "MERIDIAN_SUPERVISOR_BRANCH",
    ),
}


class MissingCredentials(Exception):
    """Raised when the env vars for a requested operator role aren't set."""


def credentials_for(role: str = "teller") -> dict:
    if role not in _CREDENTIAL_ENV:
        raise MissingCredentials(f"unknown operator role {role!r} (known: {sorted(_CREDENTIAL_ENV)})")
    op_env, pw_env, br_env = _CREDENTIAL_ENV[role]
    operator, password = os.environ.get(op_env), os.environ.get(pw_env)
    if not operator or not password:
        raise MissingCredentials(
            f"set {op_env} and {pw_env} in the environment to run a {role}-gated capability"
        )
    return {"operator": operator, "password": password,
            "branch": os.environ.get(br_env) or DEFAULT_BRANCH}


def load_signon_capability(capabilities_dir: Path = CAPABILITIES_DIR) -> Capability:
    path = capabilities_dir / f"{SIGNON_CAPABILITY_ID}.v1.json"
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found — run `python scripts/record_meridian_signon.py` first"
        )
    return Capability.model_validate_json(path.read_text())


def run_with_session(
    capability: Capability,
    params: dict,
    *,
    role: str = "teller",
    confirm: bool = False,
    headless: bool = True,
    run_id: str | None = None,
    capabilities_dir: Path = CAPABILITIES_DIR,
    risky_mode: str = "confirm",
    escalation_max_wait_s: float | None = None,
) -> Result:
    """
    Sign on as ``role``, then deterministically replay ``capability`` on the same authenticated
    browser session. Returns the target capability's ``Result``, or a ``hard_failure`` describing
    the sign-on failure if that step didn't reach the menu.
    """
    run_id = run_id or f"sess_{int(time.time() * 1000)}"
    role = role or capability.requires_role or "teller"
    try:
        creds = credentials_for(role)
    except MissingCredentials as exc:
        # A capability that requires a role we have no credentials for is an escalation, not a
        # crash — a human needs to supply them or run it themselves.
        return Result(
            status="escalated",
            failure_detail={"step_id": "signon", "expected": f"{role} credentials in the environment",
                            "observed": str(exc)},
        )
    signon = load_signon_capability(capabilities_dir)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page()
        try:
            signon_result = replay(signon, params=creds, page=page, run_id=f"{run_id}_signon")
            if signon_result.status != "success":
                return Result(
                    status="hard_failure",
                    failure_detail={
                        "step_id": "signon",
                        "expected": "operator signed on and reached the main menu",
                        "observed": f"sign-on replay returned {signon_result.status}: "
                                    f"{signon_result.failure_detail}",
                    },
                )
            # reauth: re-run the signon capability on the SAME page, so replay can recover from a
            # mid-flow session timeout (SESSION_EXPIRED / HTTP 440) instead of just reporting it.
            def _reauth() -> None:
                replay(signon, params=creds, page=page, run_id=f"{run_id}_reauth")

            return replay(capability, params=params, confirm=confirm, page=page, run_id=run_id,
                          risky_mode=risky_mode, escalation_max_wait_s=escalation_max_wait_s,
                          reauth=_reauth)
        finally:
            if not headless:
                time.sleep(5)
            browser.close()
