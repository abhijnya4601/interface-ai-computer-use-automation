"""
Pre-flight smoke test for replay/engine.py, using a hand-authored Capability that matches
exactly what agent/compiler.py would produce for lookup_member_balance (same steps, same
expected_outcomes) — NOT the deliverable capability (that one must come from a real discovery
run; see scripts/run_discovery.py). This exists purely to validate replay's mechanics for real,
against the real app, before spending API credits, and to catch bugs early exactly like
scripts/smoke_test_discovery.py did for the discovery loop.

Run: python scripts/smoke_test_replay.py   (needs the Flask app running on 5050)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.compiler import compile_capability
from artifact.schema import Checkpoint
from replay.engine import replay

BASE = "http://localhost:5050"


class FakeRecorder:
    def __init__(self, steps):
        self.steps = steps
        self.tier_log = []


def _hand_authored_steps(entry_url):
    from artifact.schema import LocatorTarget, Step

    def lt(role, name):
        return LocatorTarget(strategy="role_name", primary={"role": role, "name": name}, fallbacks=[],
                              reasoning="hand-authored for smoke test, matches real app structure")

    return [
        Step(step_id="s1", action_type="navigate", value=entry_url),
        Step(step_id="s2", action_type="type", target=lt("textbox", "Search (ID / name)"),
             value={"param_ref": "member_id"}),
        Step(step_id="s3", action_type="click", target=lt("button", "Go")),
        Step(step_id="s4", action_type="click", target=lt("link", "View")),
        Step(step_id="s5", action_type="extract", target=lt("rowheader", "Savings Balance"),
             extract_as="savings_balance"),
    ]


def _capability(target_url=f"{BASE}/search"):
    checkpoint = Checkpoint(type="element_present", locator={"role": "rowheader", "name": "Savings Balance"},
                             expected="present")
    return compile_capability(
        capability_id="lookup_member_balance", version="1.0.0", run_id="smoke_test",
        target_url=target_url, risk_level="safe",
        recorder=FakeRecorder(_hand_authored_steps(target_url)),
        outputs={"savings_balance": "placeholder"}, checkpoint=checkpoint,
    )


def check(label, condition):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        raise SystemExit(f"Smoke test failed: {label}")


def main():
    cap = _capability()

    print("=== scenario 1: success with a NEW member_id (23456, not used to record) ===")
    result = replay(cap, {"member_id": "23456"})
    print(result.model_dump())
    check("status == success", result.status == "success")
    check("savings_balance output populated", "savings_balance" in result.outputs and result.outputs["savings_balance"])

    print("\n=== scenario 2: not-seeded member_id (88888) -> business_outcome MEMBER_NOT_FOUND ===")
    result = replay(cap, {"member_id": "88888"})
    print(result.model_dump())
    check("status == business_outcome", result.status == "business_outcome")
    check("code == MEMBER_NOT_FOUND", result.business_outcome_code == "MEMBER_NOT_FOUND")

    print("\n=== scenario 3: locked member_id (99999) -> business_outcome PERMISSION_DENIED ===")
    result = replay(cap, {"member_id": "99999"})
    print(result.model_dump())
    check("status == business_outcome", result.status == "business_outcome")
    check("code == PERMISSION_DENIED", result.business_outcome_code == "PERMISSION_DENIED")

    print("\n=== scenario 4: injected hard failure (target points at a nonexistent route) ===")
    bad_cap = _capability(target_url=f"{BASE}/this-route-does-not-exist")
    result = replay(bad_cap, {"member_id": "12345"})
    print(result.model_dump())
    check("status == hard_failure", result.status == "hard_failure")
    check("failure_detail has step_id/expected/observed", result.failure_detail and
          {"step_id", "expected", "observed"} <= set(result.failure_detail.keys()))

    print("\nAll replay engine smoke checks passed.")


if __name__ == "__main__":
    main()
