"""
CLI entrypoint for the deterministic replay path:

    python scripts/run_replay.py \\
        --capability capabilities/lookup_member_balance.v1.json \\
        --params '{"member_id": "23456"}'

No LLM in the loop — replay() just walks the saved Capability's Steps. Add --confirm for a
risk_level=risky capability (e.g. open_subaccount) to actually execute past its confirmation
step; without it, a risky capability is rejected with a hard_failure before touching the page.
Add --headed to watch it run non-headless (useful for the Phase 7 escalation demo).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from artifact.schema import Capability
from guardrails.policy import redact
from replay.engine import replay

EVIDENCE_DIR = Path(__file__).parent.parent / "evidence"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capability", required=True)
    parser.add_argument("--params", required=True, help='JSON, e.g. \'{"member_id": "23456"}\'')
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--label", default=None, help="tag for the saved evidence filename")
    args = parser.parse_args()

    capability = Capability.model_validate_json(Path(args.capability).read_text())
    params = json.loads(args.params)

    result = replay(capability, params, confirm=args.confirm, headless=not args.headed)

    print(f"\nstatus: {result.status}")
    print(f"outputs: {result.outputs}")
    print(f"business_outcome_code: {result.business_outcome_code}")
    print(f"failure_detail: {result.failure_detail}")

    EVIDENCE_DIR.mkdir(exist_ok=True)
    label = args.label or f"{capability.capability_id}_{'_'.join(f'{k}-{v}' for k, v in params.items())}"
    out_path = EVIDENCE_DIR / f"replay_{label}.json"
    out_path.write_text(json.dumps(redact(result.model_dump()), indent=2))
    print(f"result saved to {out_path}")


if __name__ == "__main__":
    main()
