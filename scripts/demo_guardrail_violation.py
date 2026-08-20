"""
Phase 6 evidence generator — not a pytest test. Actually attempts an action outside the
allowlist and captures the real GuardrailViolation, so /evidence/ has proof this halts the
system rather than just an assertion in a unit test.

Run: python scripts/demo_guardrail_violation.py
"""
import json
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

from guardrails.policy import GuardrailViolation, guardrail_check

EVIDENCE_PATH = REPO_ROOT / "evidence" / "phase6_guardrail_violation.json"


def main():
    # Simulates a capability whose target was pointed at a domain outside the allowlist —
    # e.g. a tampered artifact, or a bug that let a step's target URL drift. This must be
    # caught and halted, not silently skipped.
    attempted_action = {"type": "navigate", "url": "https://not-our-bank.example.com/admin/wipe"}

    record = {
        "scenario": "capability step targets a domain outside guardrails/allowlist.yaml",
        "attempted_action": attempted_action,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    try:
        guardrail_check(attempted_action)
        record["outcome"] = "NOT_RAISED — THIS IS A BUG, the action should have been blocked"
    except GuardrailViolation as exc:
        record["outcome"] = "GuardrailViolation raised and action halted, as required"
        record["exception_type"] = type(exc).__name__
        record["exception_message"] = str(exc)
        # traceback.format_exc() bakes in absolute source paths (from sys.path); strip the repo
        # root prefix so the evidence file stays portable for a reviewer on a different machine.
        record["traceback"] = traceback.format_exc().replace(str(REPO_ROOT) + "/", "")

    EVIDENCE_PATH.parent.mkdir(exist_ok=True)
    EVIDENCE_PATH.write_text(json.dumps(record, indent=2))
    print(f"Wrote {EVIDENCE_PATH}")
    print(json.dumps(record, indent=2))


if __name__ == "__main__":
    main()
