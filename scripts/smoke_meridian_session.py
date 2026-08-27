"""
Live smoke test for agent/session.py against the real MERIDIAN CORE target — no LLM, no API key.
Proves the Phase-A3 gate: a recorded `meridian_signon` capability establishes an authenticated
session, and a *separate* target capability then replays deterministically on that same session
(the MC_SID cookie carries over — no re-login, no credentials in the target artifact).

Needs MERIDIAN_OPERATOR / MERIDIAN_PASSWORD in the environment (default branch MAIN-001):
    export MERIDIAN_OPERATOR=teller1 MERIDIAN_PASSWORD=password
    python scripts/smoke_meridian_session.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.session import credentials_for, run_with_session
from artifact.schema import Capability, Checkpoint, LocatorTarget, Step

# a minimal target capability: go straight to a member record and confirm it rendered
# (only possible if the session cookie from meridian_signon carried over)
TARGET_CAP = Capability(
    capability_id="meridian_member_record_probe",
    version="1.0.0",
    created_from_run_id="smoke",
    description="Open member 100234's record.",
    target={"app_name": "meridian-core",
            "entry_point": "https://web-sample.interface-hiring.com/members/100234",
            "surface_type": "legacy_web"},
    risk_level="safe",
    input_schema={},
    output_schema={},
    checkpoint=Checkpoint(type="text_match", locator=None, expected="Lovelace, Ada"),
    steps=[
        Step(step_id="s1", action_type="navigate",
             value="https://web-sample.interface-hiring.com/members/100234"),
        Step(step_id="s2", action_type="extract", extract_as="member_name",
             target=LocatorTarget(strategy="field_name", primary={"name": "__none__"},
                                  fallbacks=[{"strategy": "text", "text": "Lovelace, Ada"}],
                                  reasoning="probe: any resolvable anchor; the checkpoint does the real assertion")),
    ],
)

_checks: list[tuple[str, bool]] = []


def check(label: str, ok: bool) -> None:
    _checks.append((label, ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")


def main() -> int:
    try:
        creds = credentials_for("teller")
    except Exception as exc:
        print(f"  [SKIP] {exc}")
        return 2
    check("teller credentials resolved from the environment", bool(creds["operator"]))

    # 1. missing-credential path returns a clean hard_failure, not a crash
    bad = run_with_session(TARGET_CAP, params={}, role="supervisor")
    check("supervisor role with no env creds -> hard_failure (not a crash)",
          bad.status == "hard_failure" and "credentials" in str(bad.failure_detail))

    # 2. the real thing: signon capability -> authenticated target replay on one session
    result = run_with_session(TARGET_CAP, params={}, role="teller", headless=True)
    check(f"signon + target replay on one session -> success (got {result.status}; "
          f"detail={result.failure_detail})", result.status == "success")

    failed = [l for l, ok in _checks if not ok]
    print(f"\n{'ALL PASS' if not failed else f'{len(failed)} FAILED'}  "
          f"({sum(ok for _, ok in _checks)}/{len(_checks)})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
