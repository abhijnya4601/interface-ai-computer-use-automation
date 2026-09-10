"""
Replay every declared branch of a capability against the live app and record the result on the
artifact. On success (and with no unratified proposed rules), promotes lifecycle draft ->
verified.

    python scripts/verify_capability.py capabilities/lookup_member_balance.v1.json

Needs the mock app running (see README). Scenarios come from
app_knowledge/<app>.yaml -> capabilities.<id>.verify_scenarios.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.compiler import save_capability
from artifact.schema import Capability
from replay.verify import apply_verification, verify_capability


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", help="path to a capabilities/*.json file")
    parser.add_argument("--write", action="store_true",
                        help="write the VerificationRecord + lifecycle change back to the file")
    args = parser.parse_args()

    path = Path(args.artifact)
    capability = Capability.model_validate_json(path.read_text())
    print(f"verifying {capability.capability_id} (lifecycle={capability.lifecycle}, "
          f"risk={capability.risk_level})")

    record = verify_capability(capability)
    if not record.scenarios:
        print("no verify_scenarios in app_knowledge - nothing to check")
        return 1

    for row in record.scenarios:
        mark = "ok " if row["ok"] else "FAIL"
        print(f"  [{mark}] {row['params']} -> expected {row['expected_status']}"
              f"/{row['expected_code']}, observed {row['observed_status']}/{row['observed_code']}")

    updated = apply_verification(capability, record)
    print(f"\nall_passed={record.all_passed}  lifecycle: {capability.lifecycle} -> {updated.lifecycle}")

    if args.write:
        save_capability(updated, path=path)
        print(f"written to {path}")
    else:
        print("(dry run - pass --write to persist the VerificationRecord)")
        print(json.dumps(record.model_dump(), indent=2, default=str))

    return 0 if record.all_passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
