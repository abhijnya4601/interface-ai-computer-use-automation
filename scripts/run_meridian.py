"""
Session-aware deterministic replay for MERIDIAN CORE capabilities — no LLM.

Signs on as an operator (credentials from the environment: MERIDIAN_OPERATOR / MERIDIAN_PASSWORD
/ MERIDIAN_BRANCH, or MERIDIAN_SUPERVISOR_* with --role supervisor), then replays the target
capability on that same authenticated session (agent/session.run_with_session).

    python scripts/run_meridian.py \\
        --capability capabilities/meridian_check_member_balance.v1.json \\
        --params '{"member_id": "100987"}'

Add --confirm for a risk_level=risky capability, --headed to watch, --role supervisor for a
supervisor-gated capability (Place Hold). Writes a structured Result to evidence/replay_*.json.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.session import run_with_session
from artifact.schema import Capability
from guardrails.policy import redact

EVIDENCE_DIR = Path(__file__).parent.parent / "evidence"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capability", required=True)
    parser.add_argument("--params", default="{}", help='JSON, e.g. \'{"member_id": "100987"}\'')
    parser.add_argument("--role", default="teller", choices=["teller", "supervisor"])
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--label", default=None, help="tag for the saved evidence filename")
    args = parser.parse_args()

    capability = Capability.model_validate_json(Path(args.capability).read_text())
    params = json.loads(args.params)

    result = run_with_session(
        capability, params, role=args.role, confirm=args.confirm, headless=not args.headed
    )

    print(f"\nstatus: {result.status}")
    print(f"outputs: {result.outputs}")
    print(f"business_outcome_code: {result.business_outcome_code}")
    print(f"failure_detail: {result.failure_detail}")

    EVIDENCE_DIR.mkdir(exist_ok=True)
    label = args.label or (
        f"{capability.capability_id}_"
        + ("_".join(f"{k}-{v}" for k, v in params.items()) or "noparams")
    )
    out_path = EVIDENCE_DIR / f"replay_{label}.json"
    out_path.write_text(json.dumps(redact(result.model_dump()), indent=2))
    print(f"result saved to {out_path}")
    return 0 if result.status in ("success", "business_outcome", "recoverable_handled") else 1


if __name__ == "__main__":
    sys.exit(main())
